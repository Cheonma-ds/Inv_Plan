import json

from sqlalchemy.orm import Session

from backend.agents.lex.prompts import STOCK_REQUEST_TRIAGE_PROMPT
from backend.agents.lex.schemas import (
    StockPartContext,
    StockRequestIntent,
    StockRequestItem,
    StockRequestTriageInput,
    StockRequestTriageResult,
)
from backend.agents.llm_client import LLMClientProtocol
from backend.agents.tools import ToolRegistry
from backend.observability.tracer import AgentTracer


def _match_catalog_part(
    item: StockRequestItem,
    catalog_parts: list[StockPartContext],
) -> tuple[StockPartContext | None, str | None]:
    """Resolve a requested item uniquely against catalog_parts without fuzzy guesswork."""
    sku_raw = (item.part_sku or "").strip()
    name_raw = (item.part_name or "").strip()

    if sku_raw:
        sku_upper = sku_raw.upper()
        for part in catalog_parts:
            if part.part_sku.strip().upper() == sku_upper:
                return part, None
        return None, f"Requested SKU '{sku_raw}' was not found in active parts catalog"

    if name_raw:
        name_lower = name_raw.lower()
        exact = [p for p in catalog_parts if p.part_name.strip().lower() == name_lower]
        if len(exact) == 1:
            return exact[0], None
        partial = [
            p
            for p in catalog_parts
            if name_lower in p.part_name.lower() or p.part_name.lower() in name_lower
        ]
        if len(partial) == 1:
            return partial[0], None
        if len(partial) > 1:
            return None, f"Requested part name '{name_raw}' matches multiple catalog parts"
        return None, f"Requested part '{name_raw}' was not found in active parts catalog"

    return None, "Requested item is missing both part_sku and part_name"


def run_stock_request_triage(
    session: Session,
    *,
    llm: LLMClientProtocol,
    agent_name: str,
    triage_input: StockRequestTriageInput,
    confidence_threshold: float = 0.85,
    linked_comm_id: int | None = None,
    tool_registry: ToolRegistry | None = None,
) -> StockRequestTriageResult:
    """Triage a Making-team stock request email with < 0.85 confidence & SKU guardrails."""
    prompt = json.dumps(triage_input.model_dump(mode="json"), indent=2)
    trigger = f"Making stock request triage from {triage_input.sender_name}: {triage_input.subject}"

    with AgentTracer.span(
        session,
        agent_name=agent_name,
        task_type="stock_request_triage",
        trigger_summary=trigger,
        linked_entity_type="communication" if linked_comm_id else None,
        linked_entity_id=linked_comm_id,
    ) as span:
        span.input_summary = (
            f"From {triage_input.sender_persona} | Subject: {triage_input.subject}"
        )
        span.raw_prompt = prompt

        if tool_registry is not None and hasattr(llm, "generate_with_tools"):
            llm_res = llm.generate_with_tools(
                prompt=prompt,
                response_schema=StockRequestTriageResult,
                tool_registry=tool_registry,
                system_instruction=STOCK_REQUEST_TRIAGE_PROMPT,
                temperature=0.0,
            )
            span.tool_calls.extend(llm_res.tool_calls)
        else:
            llm_res = llm.generate_structured(
                prompt=prompt,
                response_schema=StockRequestTriageResult,
                system_instruction=STOCK_REQUEST_TRIAGE_PROMPT,
                temperature=0.0,
            )
        result = llm_res.parsed

        forced_reasons: list[str] = []
        if (
            result.confidence < confidence_threshold
            and result.intent != StockRequestIntent.AMBIGUOUS
        ):
            forced_reasons.append(
                f"Confidence {result.confidence:.2f} below {confidence_threshold:.2f} threshold"
            )

        normalized_items: list[StockRequestItem] = list(result.requested_items)
        if result.intent in (
            StockRequestIntent.STOCK_REQUEST,
            StockRequestIntent.MINIMUM_QTY_CONFIRMATION,
        ):
            if not result.requested_items:
                forced_reasons.append("No requested_items extracted for stock request")
            else:
                resolved_list: list[StockRequestItem] = []
                for idx, item in enumerate(result.requested_items):
                    if item.requested_qty is None or item.requested_qty <= 0:
                        forced_reasons.append(
                            f"Item {idx + 1} has missing or non-positive requested_qty"
                        )
                        resolved_list.append(item)
                        continue
                    if triage_input.catalog_parts:
                        matched_part, err = _match_catalog_part(item, triage_input.catalog_parts)
                        if matched_part is None:
                            forced_reasons.append(err or f"Item {idx + 1} failed catalog match")
                            resolved_list.append(item)
                        else:
                            resolved_list.append(
                                StockRequestItem(
                                    part_sku=matched_part.part_sku,
                                    part_name=matched_part.part_name,
                                    requested_qty=item.requested_qty,
                                )
                            )
                    else:
                        resolved_list.append(item)
                normalized_items = resolved_list

        if forced_reasons:
            reason_text = "; ".join(forced_reasons)
            new_reason = f"[Guardrail Escalation: {reason_text}] {result.reasoning}".strip()
            result = result.model_copy(
                update={
                    "intent": StockRequestIntent.AMBIGUOUS,
                    "requested_items": normalized_items,
                    "reasoning": new_reason,
                }
            )
        elif normalized_items != result.requested_items:
            result = result.model_copy(update={"requested_items": normalized_items})

        span.output_summary = (
            f"Intent={result.intent.value} | Items={len(result.requested_items)} | "
            f"Conf={result.confidence:.2f} | {result.summary_for_manager}"
        )
        span.confidence = result.confidence
        span.raw_response = llm_res.raw_text
        span.input_tokens = llm_res.input_tokens
        span.output_tokens = llm_res.output_tokens
        span.cost_inr = llm_res.cost_inr

    return result
