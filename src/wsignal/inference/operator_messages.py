from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from wsignal.models import RunEvent, Turn


def queue_message(session: AsyncSession, run_id: int, text: str) -> RunEvent:
    event = RunEvent(run_id=run_id, kind="operator_message", payload={"text": text})
    session.add(event)
    return event


def pending_messages(run_id: int):
    receipt = aliased(RunEvent)
    delivered = select(receipt.id).where(
        receipt.run_id == run_id,
        receipt.kind == "operator_message_delivered",
        receipt.payload["message_id"].as_integer() == RunEvent.id,
    ).exists()
    return (
        select(RunEvent)
        .where(
            RunEvent.run_id == run_id,
            RunEvent.kind == "operator_message",
            ~delivered,
        )
        .order_by(RunEvent.id)
    )


async def has_pending_messages(session: AsyncSession, run_id: int) -> bool:
    return await session.scalar(pending_messages(run_id).limit(1)) is not None


async def deliver_messages(session: AsyncSession, run_id: int, agent_id: int) -> None:
    events = (
        await session.scalars(pending_messages(run_id).with_for_update())
    ).all()
    if not events:
        return
    seq = await session.scalar(
        select(func.coalesce(func.max(Turn.seq), 0) + 1).where(Turn.agent_id == agent_id)
    )
    for event in events:
        text = event.payload["text"]
        session.add(Turn(
            agent_id=agent_id,
            seq=seq,
            kind="user",
            content={"content": f"Message from the operator: {text}"},
        ))
        session.add(RunEvent(
            run_id=run_id,
            kind="operator_message_delivered",
            payload={"message_id": event.id, "agent_id": agent_id, "text": text},
        ))
        seq += 1
    await session.commit()
