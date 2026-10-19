"""The shared coordinator retains aggregate bounds without starving a channel."""
import asyncio


def test_worker_observations_report_task_failure_without_exception_contents(tmp_path):
    from src.agentbus_delivery_runtime_service import DeliveryCoordinator
    from src.agentbus_service import MessageStore
    store = MessageStore(tmp_path/'observations.sqlite3')
    store.channels.configure(('C111',))
    coordinator = DeliveryCoordinator(store, {'C111':object()})
    assert coordinator.observations.snapshot()['task']['state'] == 'not_started'
    async def fail_connect():
        raise LookupError('secret-token-and-private-path')
    async def scenario():
        try:
            await coordinator.run(connect=fail_connect)
        except LookupError:
            pass
    asyncio.run(scenario())
    result = coordinator.observations.snapshot()
    assert result['task']['state'] == 'failed'
    assert result['task']['error'] == 'unexpected_task_failure'
    assert 'secret-token' not in str(result)
    assert result['delivery']['last_success_at'] is None
    store.close()


def test_worker_observations_distinguish_idle_progress_and_cancellation(tmp_path):
    from src.agentbus_delivery_runtime_service import DeliveryCoordinator
    from src.agentbus_service import MessageStore
    store = MessageStore(tmp_path/'observations.sqlite3')
    store.channels.configure(('C111',))
    coordinator = DeliveryCoordinator(store, {'C111':object()})
    async def submit():
        return None
    coordinator.submit_once = submit
    async def scenario():
        task = asyncio.create_task(coordinator.run())
        for _ in range(10):
            await asyncio.sleep(0)
            if coordinator.observations.snapshot()['delivery']['last_success_at'] is not None:
                break
        running = coordinator.observations.snapshot()
        assert running['task']['state'] == 'running'
        assert running['delivery']['last_success_at'] is not None
        assert running['federation']['state'] == 'disabled'
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    asyncio.run(scenario())
    assert coordinator.observations.snapshot()['task']['state'] == 'stopped'
    store.close()


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
    assert coordinator.cycle_incomplete is True
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
