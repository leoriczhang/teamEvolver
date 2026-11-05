# ruff: noqa: F401
from __future__ import annotations

from .deferred_evidence_review_helpers import (
    _CURLY_QUOTES,
    _STANDALONE_BULLETS,
    MAX_OUTPUT_CHARS,
    QUOTE_ALIAS_INSTRUCTIONS,
    QUOTE_REVIEW_INSTRUCTIONS,
    RESULT_SCHEMA,
    REVIEW_INSTRUCTIONS,
    REVIEW_SCHEMA,
    STAGE,
    Path,
    _exact_ids,
    _exact_quote_manifest,
    _finish_method,
    _fragment_source_quotes,
    _json,
    _method_descriptor,
    _prepare_review,
    _prepare_synthesis,
    _quote_assignment,
    _quote_chunks,
    _quote_locator_projection,
    _quote_manifest,
    _quote_original,
    _read,
    _restore_supporting_quote,
    _review_assignment,
    _review_instructions,
    _routed_review_scope,
    _save,
    _save_immutable,
    _selected_source_quotes,
    _sha,
    _support_unit,
    _synthesis_assignment,
    asyncio,
    bisect,
    chain,
    copy,
    hashlib,
    json,
    os,
    planning,
    quote_aliases,
    quote_units,
    tempfile,
    utc_now,
    validate_evidence_review,
    validate_quote_id_review,
    zip_longest,
)


class DeferredEvidenceReviewMixin:
    """Auxiliary orchestration hooks; the host owns submission, retries and cancellation."""

    async def _ensure_quote_review_compiler(self, root: Path, state: dict) -> None:
        if quote_units.QUOTE_STAGE in state.get("aux_compilers", {}):
            return
        from .compile_strategy import compiler_business_policy, render_single_stage_compiler

        base = await asyncio.to_thread((root / "compiler-base.md").read_text, encoding="utf-8")
        policy = compiler_business_policy(base)
        inherited = policy["stages"].get(STAGE, "")
        rules = (
            "Inherited custom Evidence-review business policy:\n" + inherited + "\n\n" if inherited else ""
        ) + _review_instructions(state, quote_ids=True)
        if state.get("quote_alias_protocol") == quote_aliases.PROTOCOL:
            rules += QUOTE_ALIAS_INSTRUCTIONS
        if state.get("source_unit_protocol") == "te-exact-source-units.v1":
            rules += (
                "\nThis review uses te-exact-source-review.v1. Quotes partition only the assigned complete "
                "original rows and their selected context; shared transport chunks do not authorize adjacent "
                "rows. method_input_ids identify operative quotes; context_input_ids retain scope and "
                "constraints. A positive candidate needs operative support and cannot acquire another "
                "customer, amount or task from context. Keep unresolved source contradictions explicit.\n"
            )
        body = render_single_stage_compiler(base, quote_units.QUOTE_STAGE, rules)
        from .ov_staged_orchestration import _package

        _, published = _package(body)
        state["evidence_review_quote_packet_budget"] = min(32000, 48000 - len(published) - 3072)
        if state["evidence_review_quote_packet_budget"] < 512:
            raise ValueError("Custom Evidence-review business policy leaves no bounded original-source packet budget")
        await self._ensure_aux_compiler(root, state, quote_units.QUOTE_STAGE, body)

    async def _staged_evidence_review_upgrade(self, job, root: Path, state: dict, error: str) -> dict | None:
        if self._staged_stopped(job):
            return job
        current = state.get("current") or {}

        def quote_error(text):
            return (
                isinstance(text, str)
                and text.lower().startswith(("a supporting quote", "supporting quote"))
                and any(reason in text for reason in ("literally occur", "ambiguous source offsets", "exceeding 800"))
            )

        if (
            current.get("stage") != STAGE
            or current.get("attempt") != 2
            or state.get("evidence_review_protocol") == quote_units.QUOTE_PROTOCOL
            or state.get("evidence_review_quote_upgrade")
            or not quote_error(error)
            or not quote_error(current.get("context", {}).get("repair_error"))
        ):
            return None
        predecessors = []
        for path in sorted((root / "children").glob("*/metadata.json")):
            metadata = await asyncio.to_thread(_read, path)
            if (
                metadata.get("stage") == STAGE
                and metadata.get("attempt") == 1
                and metadata.get("packet_sha256") == current["packet_sha256"]
                and metadata.get("context", {}).get("review_item_id") == current["context"]["review_item_id"]
                and metadata.get("context", {}).get("fragment_cursor") == current["context"].get("fragment_cursor")
                and metadata.get("context", {}).get("review_mode") == current["context"].get("review_mode")
                and quote_error(metadata["context"].get("repair_error"))
            ):
                predecessors.append(metadata["child"])
        if len(predecessors) != 1:
            return None
        packet = await asyncio.to_thread(_read, root / current["packet_file"])
        request = await asyncio.to_thread(_read, root / current["request_file"])
        compiler = await asyncio.to_thread((root / current["compiler_file"]).read_text, encoding="utf-8")
        if (
            _sha(_json(packet)) != current["packet_sha256"]
            or _sha(_json(request)) != current["request_sha256"]
            or _sha(compiler) != current["compiler_sha256"]
        ):
            raise ValueError("Legacy failed Evidence request changed before protocol supersession")
        raw = await asyncio.to_thread((root / "children" / current["child"] / "result.txt").read_text, encoding="utf-8")
        audit_path = root / "evidence-review" / "quote-upgrade.json"
        audit = {
            "old_current": copy.deepcopy(current),
            "old_result_sha256": _sha(raw),
            "old_result_status": "rejected",
            "previous_repair_child": predecessors[0],
            "final_error": error,
            "new_protocol": quote_units.QUOTE_PROTOCOL,
            "new_stage": quote_units.QUOTE_STAGE,
            "original_input_ids": packet["input_ids"],
        }
        repository = self._ov_skill_mining_repository()
        latest = await asyncio.to_thread(repository.get_job, job["job_id"])
        if self._staged_stopped(latest):
            return await asyncio.to_thread(
                repository.update_job, job["job_id"], status="stopped", finished_at=utc_now()
            )
        if (
            audit_path.exists()
            and (await asyncio.to_thread(_read, audit_path))["old_current"]["child"] != current["child"]
        ):
            return None
        await asyncio.to_thread(_save_immutable, audit_path, audit)
        state.update(
            evidence_review_protocol=quote_units.QUOTE_PROTOCOL,
            evidence_review_quote_upgrade=str(audit_path.relative_to(root)),
            current=None,
        )
        state.pop("evidence_review_quote_cursor", None)
        job = await self._staged_save(job, root, state, status="preparing", error="", finished_at="", ov_task_id="")
        return await self._staged_next(job, root, state)

    async def _staged_evidence_review_next(self, job: dict, root: Path, state: dict) -> dict:
        if self._staged_stopped(job):
            return job
        if not state.get("evidence_review_initialized"):
            manifest = await asyncio.to_thread(_prepare_review, root, int(state.get("proposal_count", 0)))
        else:
            manifest = await asyncio.to_thread(_read, root / "evidence-review" / "manifest.json")
        if not manifest["proposal_indexes"]:
            state.update(
                evidence_review_initialized=True,
                evidence_review_method_count=0,
                evidence_review_complete=True,
                phase="identity_group",
                current=None,
            )
            job = await self._staged_save(job, root, state)
            return await self._staged_next(job, root, state)
        if not state.get("evidence_review_initialized"):
            state.setdefault("evidence_review_protocol", quote_units.QUOTE_PROTOCOL)
            if (state.get("source_unit_protocol") == "te-exact-source-units.v1"
                    and state["evidence_review_protocol"] != quote_units.QUOTE_PROTOCOL):
                raise ValueError("Exact source review must retain its source-unit quote protocol")
            if state["evidence_review_protocol"] == quote_units.QUOTE_PROTOCOL:
                await self._ensure_quote_review_compiler(root, state)
            else:
                base = await asyncio.to_thread((root / "compiler-base.md").read_text, encoding="utf-8")
                from .compile_strategy import render_single_stage_compiler

                body = render_single_stage_compiler(base, STAGE, _review_instructions(state))
                await self._ensure_aux_compiler(root, state, STAGE, body)
            state.update(
                evidence_review_initialized=True,
                evidence_review_cursor=0,
                evidence_review_fragment_cursor=0,
                evidence_review_method_count=len(manifest["proposal_indexes"]),
                estimated_tasks=int(state.get("estimated_tasks", 0)) + 3 * manifest["maximum_groups"] + 1,
            )
            job = await self._staged_save(job, root, state)
        if state.get("evidence_review_protocol") == quote_units.QUOTE_PROTOCOL:
            await self._ensure_quote_review_compiler(root, state)
        while state["evidence_review_cursor"] < len(manifest["proposal_indexes"]):
            index = manifest["proposal_indexes"][state["evidence_review_cursor"]]
            descriptor = await asyncio.to_thread(_read, root / "evidence-review" / "methods" / f"{index:08d}.json")
            cursor = state["evidence_review_fragment_cursor"]
            if state.get("evidence_review_protocol") == quote_units.QUOTE_PROTOCOL and not state.get(
                "evidence_review_synthesis"
            ):
                await self._ensure_quote_review_compiler(root, state)
                quote_manifest = await asyncio.to_thread(_quote_manifest, root, descriptor, cursor)
                if "evidence_review_quote_cursor" not in state:
                    state["evidence_review_quote_cursor"] = quote_manifest["initial_cursor"]
                    state["estimated_tasks"] = int(state.get("estimated_tasks", 0)) + 3 * (
                        quote_manifest["unit_count"] - quote_manifest["initial_cursor"]
                    )
                    job = await self._staged_save(job, root, state)
                quote_cursor = state["evidence_review_quote_cursor"]
                if quote_cursor < quote_manifest["unit_count"]:
                    units, controls, after = await asyncio.to_thread(
                        _quote_assignment,
                        root,
                        descriptor,
                        quote_manifest,
                        quote_cursor,
                        state["language_policy"],
                        state.get("evidence_review_quote_packet_budget", 32000),
                    )
                    context = {
                        "review_proposal_index": index,
                        "review_item_id": descriptor["item"]["item_id"],
                        "quote_unit_cursor": quote_cursor,
                        "quote_unit_after": after,
                        "input_ids": [unit["id"] for unit in units],
                        "source_refs": controls["allowed_source_refs"],
                        "ordinals": list(dict.fromkeys(unit["ordinal"] for unit in units)),
                    }
                    return await self._staged_submit(
                        job, root, state, quote_units.QUOTE_STAGE, "evidence-review.json", units, controls, context
                    )
                cursor = state["evidence_review_fragment_cursor"] = len(descriptor["ordinals"])
            if cursor >= len(descriptor["ordinals"]):
                if state.get("evidence_review_synthesis"):
                    synthesis = await asyncio.to_thread(
                        _read,
                        root / "evidence-review" / "synthesis" / f"{index:08d}" / "manifest.json",
                    )
                    return await self._staged_evidence_synthesis_next(job, root, state, descriptor, synthesis)
                final = await asyncio.to_thread(_finish_method, root, descriptor, None, state["language_policy"])
                if final["outcome"] != "candidate":
                    synthesis = await asyncio.to_thread(_prepare_synthesis, root, descriptor)
                    if synthesis["unit_count"]:
                        if not state.get("evidence_review_synthesis"):
                            state["evidence_review_synthesis"] = {
                                "layer": 0,
                                "cursor": 0,
                                "unit_count": synthesis["unit_count"],
                                "output_count": 0,
                            }
                            job = await self._staged_save(job, root, state)
                        return await self._staged_evidence_synthesis_next(job, root, state, descriptor, synthesis)
                await asyncio.to_thread(
                    _save_immutable, root / "evidence-review" / "results" / f"{index:08d}.json", final
                )
                state["evidence_review_cursor"] += 1
                state["evidence_review_fragment_cursor"] = 0
                state.pop("evidence_review_quote_cursor", None)
                job = await self._staged_save(job, root, state)
                continue
            chunks, controls, after = await asyncio.to_thread(
                _review_assignment, root, descriptor, cursor, state["language_policy"]
            )
            context = {
                "review_proposal_index": index,
                "review_item_id": descriptor["item"]["item_id"],
                "fragment_cursor": cursor,
                "after": after,
                "input_ids": [chunk["id"] for chunk in chunks],
                "source_refs": controls["allowed_source_refs"],
                "ordinals": descriptor["ordinals"][cursor:after],
            }
            return await self._staged_submit(job, root, state, STAGE, "evidence-review.json", chunks, controls, context)
        state.update(evidence_review_complete=True, phase="identity_group", current=None)
        job = await self._staged_save(job, root, state)
        return await self._staged_next(job, root, state)

    async def _staged_evidence_synthesis_next(self, job, root: Path, state: dict, descriptor: dict, manifest: dict):
        progress = state["evidence_review_synthesis"]
        index = descriptor["proposal_index"]
        folder = root / "evidence-review" / "synthesis" / f"{index:08d}" / "units"
        while True:
            layer, cursor, count = progress["layer"], progress["cursor"], progress["unit_count"]
            if cursor >= count:
                if progress["output_count"] == 1:
                    terminal = await asyncio.to_thread(_read, folder / str(layer + 1) / "00000000.json")
                    final = await asyncio.to_thread(
                        _finish_method, root, descriptor, terminal, state["language_policy"]
                    )
                    await asyncio.to_thread(
                        _save_immutable, root / "evidence-review" / "results" / f"{index:08d}.json", final
                    )
                    state["evidence_review_cursor"] += 1
                    state["evidence_review_fragment_cursor"] = 0
                    state.pop("evidence_review_quote_cursor", None)
                    state.pop("evidence_review_synthesis", None)
                    job = await self._staged_save(job, root, state)
                    return await self._staged_evidence_review_next(job, root, state)
                if layer + 1 >= manifest["maximum_layers"] or progress["output_count"] >= count:
                    raise ValueError("Bounded Evidence support combination must reduce its pending unit count")
                progress.update(layer=layer + 1, cursor=0, unit_count=progress["output_count"], output_count=0)
                job = await self._staged_save(job, root, state)
                continue
            if count > 1 and count - cursor == 1:
                unit = await asyncio.to_thread(_read, folder / str(layer) / f"{cursor:08d}.json")
                await asyncio.to_thread(
                    _save_immutable, folder / str(layer + 1) / f"{progress['output_count']:08d}.json", unit
                )
                progress["cursor"] += 1
                progress["output_count"] += 1
                job = await self._staged_save(job, root, state)
                continue
            evidence, controls, after, unit_ids = await asyncio.to_thread(
                _synthesis_assignment,
                root,
                descriptor,
                layer,
                cursor,
                count,
                state["language_policy"],
                quote_units.QUOTE_STAGE
                if state.get("evidence_review_protocol") == quote_units.QUOTE_PROTOCOL
                else STAGE,
                state.get("evidence_review_quote_packet_budget", 32000),
            )
            if not evidence:
                units = [
                    await asyncio.to_thread(_read, folder / str(layer) / f"{position:08d}.json")
                    for position in range(cursor, after)
                ]
                sources = list(dict.fromkeys(ref for unit in units for ref in unit["review"]["source_refs"]))
                reasons = list(dict.fromkeys(unit["review"]["reason"] for unit in units))
                no_support_reason = (
                    "已复核的支持单元均未留下可组合的完整方法支持：" + "；".join(reasons)
                    if state["language_policy"].get("language") == "zh-CN"
                    else "The reviewed support units contain no combinable support for a complete method: "
                    + "; ".join(reasons)
                )
                row = {
                    "item_id": descriptor["item"]["item_id"],
                    "outcome": "insufficient_this_fragment",
                    "reason": no_support_reason[:1000],
                    "source_refs": sources,
                    "supporting_quotes": [],
                    "partial_steps": [],
                    "candidate": None,
                }
                unit = {
                    "id": "review-unit-" + _sha(_json([index, layer, unit_ids, "no-support"]))[:24],
                    "review": row,
                    "quotes": [],
                    "parents": unit_ids,
                    "ordinals": sorted({ordinal for item in units for ordinal in item["ordinals"]}),
                }
                await asyncio.to_thread(
                    _save_immutable, folder / str(layer + 1) / f"{progress['output_count']:08d}.json", unit
                )
                progress["cursor"] = after
                progress["output_count"] += 1
                job = await self._staged_save(job, root, state)
                continue
            context = {
                "review_mode": "combine",
                "review_proposal_index": index,
                "review_item_id": descriptor["item"]["item_id"],
                "review_layer": layer,
                "review_unit_cursor": cursor,
                "review_unit_after": after,
                "review_output_index": progress["output_count"],
                "support_unit_ids": unit_ids,
                "input_ids": [chunk["id"] for chunk in evidence],
                "source_refs": controls["allowed_source_refs"],
            }
            return await self._staged_submit(
                job,
                root,
                state,
                quote_units.QUOTE_STAGE
                if state.get("evidence_review_protocol") == quote_units.QUOTE_PROTOCOL
                else STAGE,
                "evidence-review.json",
                evidence,
                controls,
                context,
            )

    async def _staged_evidence_review_accept(self, job, root: Path, state: dict, current: dict, value) -> None:
        context = current["context"]
        index = context["review_proposal_index"]
        descriptor = await asyncio.to_thread(_read, root / "evidence-review" / "methods" / f"{index:08d}.json")
        model_limit = MAX_OUTPUT_CHARS
        if current.get("quote_alias_protocol") is not None:
            from .direct_workflow_planning import FrozenPlanningIntegrityError

            if (current["quote_alias_protocol"] != quote_aliases.PROTOCOL
                    or current.get("stage") != quote_units.QUOTE_STAGE):
                raise FrozenPlanningIntegrityError("Frozen Evidence quote alias protocol changed")
            packet, manifest = await asyncio.to_thread(
                self._staged_canonical_packet_for_current, root, state, current
            )
            raw_chars = len(_json(value))
            try:
                value = quote_aliases.decode_quote_alias_output(value, manifest)
            except quote_aliases.QuoteAliasManifestError as error:
                raise FrozenPlanningIntegrityError(str(error)) from error
            # The adapter already enforces the model's unchanged 12K output bound.
            # Only exact host-restored machine IDs may expand that bounded response.
            model_limit += max(0, len(_json(value)) - raw_chars)
        else:
            packet = await asyncio.to_thread(_read, root / current["packet_file"])
            if _sha(_json(packet)) != current["packet_sha256"]:
                raise ValueError("Frozen Evidence review packet changed")
        controls, evidence = packet["derived_controls"], packet["evidence"]
        id_protocol = current.get("stage") == quote_units.QUOTE_STAGE
        if (
            context["review_item_id"] != descriptor["item"]["item_id"]
            or [item["item_id"] for item in controls["items"]] != [context["review_item_id"]]
            or [chunk["id"] for chunk in evidence] != context["input_ids"]
        ):
            raise ValueError("Evidence review assignment changed its durable method or raw scope")
        if context.get("review_mode") == "combine":
            expected, expected_controls, after, unit_ids = await asyncio.to_thread(
                _synthesis_assignment,
                root,
                descriptor,
                context["review_layer"],
                context["review_unit_cursor"],
                state["evidence_review_synthesis"]["unit_count"],
                state["language_policy"],
                quote_units.QUOTE_STAGE if id_protocol else STAGE,
                state.get("evidence_review_quote_packet_budget", 32000),
            )
            if (
                evidence != expected
                or controls != expected_controls
                or context["review_unit_after"] != after
                or context["support_unit_ids"] != unit_ids
            ):
                raise ValueError("Evidence support combination changed its frozen quote ancestry")
            restorations, source_audit = [], []
            if id_protocol:
                checked, source_audit = validate_quote_id_review(
                    value, items=controls["items"], evidence=evidence, _max_output_chars=model_limit
                )
                from .source_unit_review import validate_positive_support

                validate_positive_support(descriptor, checked["reviews"][0], source_audit)
            else:
                checked = validate_evidence_review(
                    value, items=controls["items"], evidence=evidence, restorations=restorations
                )
            quotes = _selected_source_quotes(evidence, source_audit) if id_protocol else None
            unit = _support_unit(checked["reviews"][0], evidence, current["child"], unit_ids, quotes=quotes)
            if id_protocol:
                unit.update(quotation_protocol=quote_units.QUOTE_PROTOCOL, source_quote_audit=source_audit)
            if restorations:
                unit.update(format_restoration=True, quote_restorations=restorations)
            # Keep every parent's audit pointer, including inputs not selected as representative quotes.
            folder = root / "evidence-review" / "synthesis" / f"{index:08d}"
            await asyncio.to_thread(_save_immutable, folder / "calls" / current["child"], checked)
            await asyncio.to_thread(
                _save_immutable,
                folder / "units" / str(context["review_layer"] + 1) / f"{context['review_output_index']:08d}.json",
                unit,
            )
            progress = state["evidence_review_synthesis"]
            progress["cursor"] = after
            progress["output_count"] = context["review_output_index"] + 1
            return
        if id_protocol:
            manifest = await asyncio.to_thread(
                _quote_manifest, root, descriptor, state["evidence_review_fragment_cursor"]
            )
            expected, expected_controls, after = await asyncio.to_thread(
                _quote_assignment,
                root,
                descriptor,
                manifest,
                context["quote_unit_cursor"],
                state["language_policy"],
                state.get("evidence_review_quote_packet_budget", 32000),
            )
            if evidence != expected or controls != expected_controls or context["quote_unit_after"] != after:
                raise ValueError("Frozen source quote assignment changed its complete unit coverage")
            for unit in evidence:
                original = await asyncio.to_thread(_quote_original, root, unit)
                quote_units.validate_source_quote_unit(unit, original)
            checked, audit = validate_quote_id_review(
                value, items=controls["items"], evidence=evidence, _max_output_chars=model_limit
            )
            from .source_unit_review import validate_positive_support

            validate_positive_support(descriptor, checked["reviews"][0], audit)
            fragment = {
                "input_ids": context["input_ids"],
                "ordinals": context["ordinals"],
                "review": checked["reviews"][0],
                "child": current["child"],
                "quotation_protocol": quote_units.QUOTE_PROTOCOL,
                "source_quote_audit": audit,
            }
            await asyncio.to_thread(
                _save_immutable,
                root
                / "evidence-review"
                / "quote-fragments"
                / f"{index:08d}"
                / f"{context['quote_unit_cursor']:08d}.json",
                fragment,
            )
            state["evidence_review_quote_cursor"] = after
            return
        if context["ordinals"] != descriptor["ordinals"][context["fragment_cursor"] : context["after"]] or len(
            context["ordinals"]
        ) != len(evidence):
            raise ValueError("Evidence review raw scope changed")
        for ordinal, chunk in zip(context["ordinals"], evidence):
            frozen = await asyncio.to_thread(_read, root / "chunks" / f"{ordinal:08d}.json")
            if frozen != chunk or _sha(chunk["content"]) != chunk["sha256"]:
                raise ValueError("Evidence review input does not match the frozen original")
        restorations = []
        checked = validate_evidence_review(value, items=controls["items"], evidence=evidence, restorations=restorations)
        fragment = {
            "input_ids": context["input_ids"],
            "ordinals": context["ordinals"],
            "review": checked["reviews"][0],
            "child": current["child"],
        }
        if restorations:
            fragment.update(format_restoration=True, quote_restorations=restorations)
        await asyncio.to_thread(
            _save_immutable,
            root / "evidence-review" / "fragments" / f"{index:08d}" / f"{context['fragment_cursor']:08d}.json",
            fragment,
        )
        state["evidence_review_fragment_cursor"] = context["after"]
