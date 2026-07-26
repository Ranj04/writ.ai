from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from writai.intake.replay import (
    JsonSlackDeliveryReplayStore,
    SlackDeliveryKey,
)


def _key() -> SlackDeliveryKey:
    return SlackDeliveryKey(
        workspace_id="csv-exports",
        connection_user_id="writai-user",
        trigger_kind="SLACK_CHANNEL_MESSAGE_RECEIVED",
        event_id="msg-delivery-1",
    )


def test_delivery_reservation_is_concurrent_and_restart_safe(
    tmp_path: Path,
) -> None:
    path = tmp_path / "slack-deliveries.json"
    store = JsonSlackDeliveryReplayStore(path)

    with ThreadPoolExecutor(max_workers=8) as executor:
        reservations = list(
            executor.map(lambda _item: store.reserve(_key()), range(16))
        )

    assert reservations.count(True) == 1
    assert reservations.count(False) == 15
    assert not JsonSlackDeliveryReplayStore(path).reserve(_key())
    assert path.stat().st_mode & 0o777 == 0o600


def test_completed_delivery_result_is_idempotent_and_immutable(
    tmp_path: Path,
) -> None:
    store = JsonSlackDeliveryReplayStore(tmp_path / "slack-deliveries.json")
    assert store.reserve(_key())
    result = {
        "status": "pending-human-confirmation",
        "decision_id": "DEC-018",
    }

    store.complete(_key(), result=result)
    store.complete(_key(), result=result)

    restarted = JsonSlackDeliveryReplayStore(store.path)
    record = restarted.get(_key())
    assert record is not None
    assert record.status == "completed"
    assert record.result == result
