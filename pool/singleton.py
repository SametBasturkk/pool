from typing import Dict, List, Optional, Tuple
import logging

from blspy import G2Element

from chia.consensus.coinbase import pool_parent_id
from chia.consensus.constants import ConsensusConstants
from chia.pools.pool_puzzles import (
    create_absorb_spend,
    solution_to_pool_state,
    get_most_recent_singleton_coin_from_coin_spend,
    pool_state_to_inner_puzzle,
    create_full_puzzle,
    get_delayed_puz_info_from_launcher_spend,
)
from chia.pools.pool_wallet import PoolSingletonState
from chia.pools.pool_wallet_info import PoolState
from chia.rpc.full_node_rpc_client import FullNodeRpcClient
from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import Program
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.types.coin_record import CoinRecord
from chia.types.coin_spend import CoinSpend
from chia.types.spend_bundle import SpendBundle
from chia.util.ints import uint32, uint64

from .absorb_spend import spend_with_fee
from .record import FarmerRecord

logger = logging.getLogger('singleton')


class LastSpendCoinNotFound(Exception):
    def __init__(self, last_not_none_state):
        self.last_not_none_state = last_not_none_state


async def get_coin_spend(node_rpc_client: FullNodeRpcClient, coin_record: CoinRecord) -> Optional[CoinSpend]:
    if not coin_record.spent:
        return None
    return await node_rpc_client.get_puzzle_and_solution(coin_record.coin.name(), coin_record.spent_block_index)


def validate_puzzle_hash(
    launcher_id: bytes32,
    delay_ph: bytes32,
    delay_time: uint64,
    pool_state: PoolState,
    outer_puzzle_hash: bytes32,
    genesis_challenge: bytes32,
) -> bool:
    inner_puzzle: Program = pool_state_to_inner_puzzle(pool_state, launcher_id, genesis_challenge, delay_time, delay_ph)
    new_full_puzzle: Program = create_full_puzzle(inner_puzzle, launcher_id)
    return new_full_puzzle.get_tree_hash() == outer_puzzle_hash


async def get_singleton_state(
    node_rpc_client: FullNodeRpcClient,
    launcher_id: bytes32,
    farmer_record: Optional[FarmerRecord],
    peak_height: uint32,
    confirmation_security_threshold: int,
    genesis_challenge: bytes32,
    raise_exc=False,
) -> Optional[Tuple[CoinSpend, PoolState, PoolState]]:
    try:
        if farmer_record is None:
            launcher_coin: Optional[CoinRecord] = await node_rpc_client.get_coin_record_by_name(launcher_id)
            if launcher_coin is None:
                logger.warning(f"Can not find genesis coin {launcher_id}")
                return None
            if not launcher_coin.spent:
                logger.warning(f"Genesis coin {launcher_id} not spent")
                return None

            last_spend: Optional[CoinSpend] = await get_coin_spend(node_rpc_client, launcher_coin)
            delay_time, delay_puzzle_hash = get_delayed_puz_info_from_launcher_spend(last_spend)
            saved_state = solution_to_pool_state(last_spend)
            assert last_spend is not None and saved_state is not None
        else:
            last_spend = farmer_record.singleton_tip
            saved_state = farmer_record.singleton_tip_state
            delay_time = farmer_record.delay_time
            delay_puzzle_hash = farmer_record.delay_puzzle_hash

        saved_spend = last_spend
        last_not_none_state: PoolState = saved_state
        assert last_spend is not None

        last_coin_record: Optional[CoinRecord] = await node_rpc_client.get_coin_record_by_name(last_spend.coin.name())
        if last_coin_record is None:
            if raise_exc:
                raise LastSpendCoinNotFound(last_not_none_state)
            logger.info('Last spend coin record for %s is None', launcher_id.hex())
            if last_not_none_state:
                logger.info('Last pool url %s', last_not_none_state.pool_url)
            return None

        while True:
            # Get next coin solution
            next_coin: Optional[Coin] = get_most_recent_singleton_coin_from_coin_spend(last_spend)
            if next_coin is None:
                # This means the singleton is invalid
                return None
            next_coin_record: Optional[CoinRecord] = await node_rpc_client.get_coin_record_by_name(next_coin.name())
            assert next_coin_record is not None

            if not next_coin_record.spent:
                if not validate_puzzle_hash(
                    launcher_id,
                    delay_puzzle_hash,
                    delay_time,
                    last_not_none_state,
                    next_coin_record.coin.puzzle_hash,
                    genesis_challenge,
                ):
                    logger.warning(f"Invalid singleton puzzle_hash for {launcher_id}")
                    return None
                break

            last_spend: Optional[CoinSpend] = await get_coin_spend(node_rpc_client, next_coin_record)
            assert last_spend is not None

            pool_state: Optional[PoolState] = solution_to_pool_state(last_spend)

            if pool_state is not None:
                last_not_none_state = pool_state
            if peak_height - confirmation_security_threshold >= next_coin_record.spent_block_index:
                # There is a state transition, and it is sufficiently buried
                saved_spend = last_spend
                saved_state = last_not_none_state

        return saved_spend, saved_state, last_not_none_state
    except LastSpendCoinNotFound:
        raise
    except Exception as e:
        logger.error(f"Error getting singleton: {e}", exc_info=True)
        return None


def get_farmed_height(reward_coin_record: CoinRecord, genesis_challenge: bytes32) -> Optional[uint32]:
    # Returns the height farmed if it's a coinbase reward, otherwise None
    for block_index in range(
        reward_coin_record.confirmed_block_index, reward_coin_record.confirmed_block_index - 128, -1
    ):
        if block_index < 0:
            break
        pool_parent = pool_parent_id(uint32(block_index), genesis_challenge)
        if pool_parent == reward_coin_record.coin.parent_coin_info:
            return uint32(block_index)
    return None


async def create_absorb_transaction(
    node_rpc_client: FullNodeRpcClient,
    wallets: List[Dict],
    farmer_record: FarmerRecord,
    peak_height: uint32,
    reward_coin_records: List[CoinRecord],
    fee,
    constants: ConsensusConstants,
) -> Optional[SpendBundle]:
    singleton_state_tuple: Optional[Tuple[CoinSpend, PoolState, PoolState]] = await get_singleton_state(
        node_rpc_client, farmer_record.launcher_id, farmer_record, peak_height, 0, constants.GENESIS_CHALLENGE
    )
    if singleton_state_tuple is None:
        logger.info(f"Invalid singleton {farmer_record.launcher_id}.")
        return None
    last_spend, last_state, last_state_2 = singleton_state_tuple
    # Here the buried state is equivalent to the latest state, because we use 0 as the security_threshold
    assert last_state == last_state_2

    if last_state.state == PoolSingletonState.SELF_POOLING:
        logger.info(f"Don't try to absorb from former farmer {farmer_record.launcher_id}.")
        return None

    launcher_coin_record: Optional[CoinRecord] = await node_rpc_client.get_coin_record_by_name(
        farmer_record.launcher_id
    )
    assert launcher_coin_record is not None

    all_spends: List[CoinSpend] = []
    for reward_coin_record in reward_coin_records:
        found_block_index: Optional[uint32] = get_farmed_height(reward_coin_record, constants.GENESIS_CHALLENGE)
        if not found_block_index:
            # The puzzle does not allow spending coins that are not a coinbase reward
            logger.info(f"Received reward {reward_coin_record.coin} that is not a pool reward.")
            continue

        absorb_spend: List[CoinSpend] = create_absorb_spend(
            last_spend,
            last_state,
            launcher_coin_record.coin,
            found_block_index,
            constants.GENESIS_CHALLENGE,
            farmer_record.delay_time,
            farmer_record.delay_puzzle_hash,
        )
        last_spend = absorb_spend[0]
        all_spends += absorb_spend

    if len(all_spends) == 0:
        return None

    if fee:
        return await spend_with_fee(node_rpc_client, wallets, all_spends, constants)
    else:
        return SpendBundle(all_spends, G2Element())


async def find_singleton_from_coin(
    node_rpc_client: FullNodeRpcClient, store, blockchain_height: int, coin: CoinRecord,
    scan_phs: List[bytes32]
):

    coin_records: List[CoinRecord] = await node_rpc_client.get_coin_records_by_puzzle_hashes(
        scan_phs,
        include_spent_coins=True,
        start_height=coin.confirmed_block_index - 1000,
        end_height=coin.confirmed_block_index,
    )

    singleton_name: bytes32 = coin.coin.parent_coin_info

    for c in sorted(coin_records, key=lambda x: int(x.confirmed_block_index), reverse=True):
        if not c.coinbase:
            continue

        if not c.spent:
            continue

        farmer = await store.get_farmer_records_for_p2_singleton_phs([c.coin.puzzle_hash])
        if not farmer:
            continue
        farmer = farmer[0]

        singleton_tip: Optional[Coin] = get_most_recent_singleton_coin_from_coin_spend(
            farmer.singleton_tip
        )
        if singleton_tip is None:
            continue

        singleton_coin_record: Optional[
            CoinRecord
        ] = await node_rpc_client.get_coin_record_by_name(singleton_tip.name())

        for i in range(10):

            if not singleton_coin_record:
                break

            if singleton_coin_record.name == singleton_name:
                return (c, singleton_coin_record, farmer)

            singleton_coin_record = await node_rpc_client.get_coin_record_by_name(
                singleton_coin_record.coin.parent_coin_info
            )
