"""The shared coordinator retains aggregate bounds without starving a channel."""
import asyncio

import pytest


@pytest.mark.parametrize('phase', ['delivery','federation'])
@pytest.mark.parametrize('outcome', ['cancel','error'])
def test_terminal_task_reconciles_mid_cycle_observations_without_false_success(tmp_path, phase, outcome):
    from types import SimpleNamespace

    from src.agentbus_delivery_runtime_service import DeliveryCoordinator
    from src.agentbus_service import MessageStore
    store = MessageStore(tmp_path/'mid-cycle.sqlite3')
    store.channels.configure(('C111',))
    coordinator = DeliveryCoordinator(store, {'C111':object()})
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        async def blocked():
            entered.set()
            await release.wait()
            raise LookupError('private-provider-token')
        async def submit():
            if phase == 'delivery':
                await blocked()
        coordinator.submit_once = submit
        bridge = SimpleNamespace(cycle=blocked) if phase == 'federation' else None
        task = asyncio.create_task(coordinator.run(bridge))
        await asyncio.wait_for(entered.wait(), timeout=1)
        assert coordinator.observations.snapshot()[phase]['state'] == 'running'
        if outcome == 'cancel':
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            release.set()
            with pytest.raises(LookupError):
                await task
    asyncio.run(scenario())
    result = coordinator.observations.snapshot()
    assert result['task']['state'] == ('stopped' if outcome == 'cancel' else 'failed')
    assert result[phase]['state'] == ('interrupted' if outcome == 'cancel' else 'failed')
    assert result[phase]['last_completed_at'] is None
    assert not any(row['state'] == 'running' for row in result.values())
    assert 'private-provider-token' not in str(result)
    store.close()


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
    assert result['delivery']['last_completed_at'] is None
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
            if coordinator.observations.snapshot()['delivery']['last_completed_at'] is not None:
                break
        running = coordinator.observations.snapshot()
        assert running['task']['state'] == 'running'
        assert running['delivery']['last_completed_at'] is not None
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
