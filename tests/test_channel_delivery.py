"""The shared coordinator retains aggregate bounds without starving a channel."""
import asyncio


def test_coordinator_rotates_starting_channel_with_one_aggregate_budget(tmp_path):
    from src.agentbus_delivery_runtime_service import DeliveryCoordinator
    from src.agentbus_service import MessageStore
    store=MessageStore(tmp_path/'bus.sqlite3')
    store.channels.configure(('C111','C222'))
    coordinator=DeliveryCoordinator(store,{'C111':object(),'C222':object()})
    visits=[]
    for worker in coordinator.workers:
        async def submit(*,budget,deadline,channel=worker.channel):
            visits.append((channel,budget))
            return budget
        worker.submit_once=submit
    asyncio.run(coordinator.submit_once())
    asyncio.run(coordinator.submit_once())
    assert visits==[('C111',8),('C222',8)]
    store.close()


def test_channel_failure_does_not_abort_other_channel_cycle(tmp_path):
    from src.agentbus_delivery_runtime_service import DeliveryCoordinator
    from src.agentbus_service import MessageStore
    store=MessageStore(tmp_path/'bus.sqlite3')
    store.channels.configure(('C111','C222'))
    coordinator=DeliveryCoordinator(store,{'C111':object(),'C222':object()})
    visits=[]
    async def fail(**kwargs):
        raise ValueError('isolated channel provider failure')
    async def succeed(**kwargs):
        visits.append('C222')
        return 0
    coordinator.workers[0].submit_once=fail
    coordinator.workers[1].submit_once=succeed
    asyncio.run(coordinator.submit_once())
    assert visits==['C222']
    store.close()


def test_channel_failure_after_submission_keeps_aggregate_attempt_budget(tmp_path):
    from src.agentbus_delivery_runtime_service import DeliveryCoordinator
    from src.agentbus_service import MessageStore
    store = MessageStore(tmp_path/'attempt-budget.sqlite3')
    store.channels.configure(('C111','C222'))
    coordinator = DeliveryCoordinator(store, {'C111':object(),'C222':object()})
    budgets = []
    async def partial(**kwargs):
        coordinator.workers[0].cycle_attempts = 3
        raise OSError('lost local completion after three reserved provider attempts')
    async def following(**kwargs):
        budgets.append(kwargs['budget'])
        return 0
    coordinator.workers[0].submit_once = partial
    coordinator.workers[1].submit_once = following
    asyncio.run(coordinator.submit_once())
    assert budgets == [5]
    store.close()
