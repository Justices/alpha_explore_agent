"""Field-by-field Regular Alpha plan and bounded execution.

This collaborator owns only construction/repair and child batch sequencing.
It delegates all gateway writes and recovery to the existing ResearchAgent
Simulation Intent controls, and never invokes a model directly.
"""
import hashlib
import json

from alpha_research.expressions.validator import validate
from alpha_research.state.store import atomic_write_json, read_json
from alpha_research.progress import write_progress
from alpha_research.workflows.regular.errors import ResearchError
from alpha_research.workflows.regular.field_catalog import _field_summaries


def _emit(execution_context, event_type, **payload):
    callback = (execution_context or {}).get("emit_event")
    if callable(callback):
        callback(event_type, payload)


class FieldPlanWorkflow:
    def __init__(self, agent):
        # Agent is injected as the owner of simulation permissions and
        # immutable execution intent recovery; this class is not a gateway.
        self.agent = agent

    def construct(self, batch_dir, context, core_fields, field_rows,
                              catalog, operators, operator_catalog, prior_case,
                              *, execution_context=None):
        dataset_id = context["dataset_id"]
        description = context["dataset_selection"].get("description")
        settings = self.agent._platform_settings(context["scope"])
        seen = {expression for batch in prior_case.get("batches", [])
                if batch.get("dataset_id") == dataset_id
                for expression in batch.get("expressions", [])}
        screened = set(core_fields)
        vector_adapters = sorted(name for name in operators if name.startswith("vec_"))
        variants, validation, failures = [], {}, []
        for start in range(0, len(core_fields), 8):
            targets = core_fields[start:start + 8]
            feedback = {}
            for attempt in range(2):
                if self.agent.stop_flag and self.agent.stop_flag.exists():
                    raise ResearchError("stopped before the field plan was completed")
                request = {
                    "objective": context["objective"], "scope": context["scope"],
                    "simulation_settings": settings, "dataset_id": dataset_id,
                    "dataset_description": description, "target_field_ids": targets,
                    "fields": _field_summaries([field_rows[field_id] for field_id in targets]),
                    "vector_field_ids": [field_id for field_id in targets
                                         if catalog[field_id]["type"] == "VECTOR"],
                    "vector_adapters": vector_adapters,
                    "operator_catalog": operator_catalog,
                    "prior_expressions": sorted(seen),
                }
                if feedback:
                    request["validation_feedback"] = [
                        error for errors in feedback.values() for error in errors]
                if context.get("prior_research"):
                    request["prior_campaign_findings"] = context["prior_research"]
                try:
                    response = self.agent._decide("construct_field_expressions", request, batch_dir=batch_dir)
                    _emit(execution_context, "regular.field_expression_proposal_generated",
                          research_id=self.agent.recorder.research_id,
                          dataset_id=dataset_id, batch_ref=str(batch_dir),
                          target_field_ids=targets, attempt=attempt + 1,
                          model_session_id=self.agent.recorder.session_id,
                          model_call_id=self.agent.recorder.call_id,
                          proposal_sha256=hashlib.sha256(json.dumps(
                              response, ensure_ascii=False, sort_keys=True,
                              separators=(",", ":")).encode("utf-8")).hexdigest())
                    rows = response.get("fields") if isinstance(response, dict) else None
                    if not isinstance(rows, list):
                        raise ResearchError("field construction must return a fields list")
                except Exception as exc:
                    for field_id in targets:
                        originals = feedback.get(field_id) or [{"field_id": field_id}]
                        failures.extend({**row, "repair_error": str(exc)} if attempt else
                                        {**row, "reason": str(exc)} for row in originals)
                    break
                rejected = {}
                for field_id in targets:
                    proposals = [row for row in rows if isinstance(row, dict)
                                 and row.get("field_id") == field_id]
                    if len(proposals) != 1:
                        failures.extend(feedback.get(field_id) or [
                            {"field_id": field_id, "reason": "missing or repeated field proposal"}])
                        continue
                    proposed = proposals[0].get("variants")
                    originals = feedback.get(field_id, [])
                    limit = len(originals) if attempt else 3
                    if not isinstance(proposed, list) or not 1 <= len(proposed) <= limit:
                        reason = proposals[0].get("unresolved_reason") or f"expected one to {limit} variants"
                        if not attempt and proposed == [] and (
                                catalog[field_id]["type"] != "VECTOR" or vector_adapters):
                            rejected[field_id] = [{"field_id": field_id, "expression": None,
                                                   "reason": reason}]
                        else:
                            failures.extend([{**row, "repair_error": reason} for row in originals]
                                            or [{"field_id": field_id, "reason": reason}])
                        continue
                    for variant in proposed:
                        if not isinstance(variant, dict):
                            variant = {}
                        expression = variant.get("expression")
                        reason = variant.get("reason")
                        research = variant.get("research")
                        failure = None
                        if (not isinstance(expression, str) or not expression.strip()
                                or not isinstance(reason, str) or not reason.strip()
                                or not self.agent._has_complete_runner_research(research)):
                            failure = "missing expression or per-variant research"
                        elif expression in seen:
                            failure = "duplicate or previously tested expression"
                        else:
                            verdict = validate(expression, dataset_id, catalog, operators,
                                               operator_definitions=operator_catalog)
                            if verdict["verdict"] != "PASS":
                                failure = verdict["errors"]
                            elif field_id not in verdict["fields"] or set(verdict["fields"]) - screened:
                                failure = "expression must reference its target and only screened fields"
                        if failure:
                            rejection = {"field_id": field_id, "expression": expression, "reason": failure}
                            if not attempt:
                                rejection.update(variant_reason=reason, research=research)
                            rejected.setdefault(field_id, []).append(rejection)
                            continue
                        seen.add(expression)
                        validation[expression] = verdict
                        variants.append({"field_id": field_id, "expression": expression,
                                         "reason": reason, "research": research})
                    failures.extend(originals[len(proposed):])
                if attempt:
                    failures.extend(error for errors in rejected.values() for error in errors)
                elif rejected:
                    feedback = rejected
                    targets = list(feedback)
                    write_progress(f"{dataset_id} · 正在反馈未生成或未通过校验的表达式")
                else:
                    break
        plan = {"schema_version": 1, "dataset_id": dataset_id, "simulation_settings": settings,
                "target_field_ids": core_fields, "variants": variants,
                "validation": validation, "field_failures": failures}
        atomic_write_json(batch_dir / "field_research_plan.json", plan)
        _emit(execution_context, "regular.field_plan_persisted",
              research_id=self.agent.recorder.research_id,
              dataset_id=dataset_id, batch_ref=str(batch_dir),
              target_field_count=len(core_fields), variant_count=len(variants),
              failure_count=len(failures),
              field_plan_ref=str(batch_dir / "field_research_plan.json"),
              field_plan_sha256=hashlib.sha256(
                  (batch_dir / "field_research_plan.json").read_bytes()).hexdigest())
        write_progress(f"{dataset_id} · 已保存 {len(core_fields)} 个字段的设计结果 · "
                       f"{len(variants)} 条表达式通过校验 · {len(failures)} 项未通过")
        return self.agent._execute_field_plan(
            batch_dir, context, execution_context=execution_context)

    def execute(self, batch_dir, context, *, execution_context=None):
        plan_path = batch_dir / "field_research_plan.json"
        plan_bytes = plan_path.read_bytes()
        plan = json.loads(plan_bytes)
        digest = hashlib.sha256(plan_bytes).hexdigest()
        _emit(execution_context, "regular.field_plan_execution_started",
              research_id=context.get("research_id"), dataset_id=context.get("dataset_id"),
              batch_ref=str(batch_dir), field_plan_ref=str(plan_path),
              field_plan_sha256=digest, planned_variant_count=len(plan.get("variants", [])))
        dataset_id, scope = context["dataset_id"], context["scope"]
        settings = plan["simulation_settings"]
        if plan["dataset_id"] != dataset_id or settings != self.agent._platform_settings(scope):
            raise ResearchError("field plan does not match its saved scope and settings")
        plan_variants = plan["variants"]
        case_key, run_id = context["platform_case_key"], context["platform_run_id"]
        completed, failures = [], []
        stopped = False
        for start in range(0, len(plan_variants), 8):
            if self.agent.stop_flag and self.agent.stop_flag.exists():
                stopped = True
                break
            group = plan_variants[start:start + 8]
            index = start // 8 + 1
            suffix = hashlib.sha256(f"{batch_dir.name}:{index}".encode()).hexdigest()[:8]
            child = batch_dir / "execution" / f"{batch_dir.name[:-8]}{suffix}"
            child_context = {key: context[key] for key in (
                "objective", "research_type", "scope", "dataset_id", "research_id",
                "platform_case_key", "platform_run_id", "started_at")}
            child_context.update(field_plan_ref=str(plan_path), field_plan_sha256=digest,
                                 execution_group=index)
            context_path = child / "batch_context.json"
            if context_path.exists():
                if read_json(context_path) != child_context:
                    raise ResearchError("execution group no longer matches the saved field plan")
            else:
                if child.exists() and any(child.iterdir()):
                    raise ResearchError("execution group is missing its saved context")
                atomic_write_json(context_path, child_context)
            _emit(execution_context, "regular.field_execution_group_started",
                  research_id=context.get("research_id"), dataset_id=dataset_id,
                  batch_ref=str(batch_dir), child_batch_ref=str(child),
                  execution_group=index, variant_count=len(group),
                  field_plan_ref=str(plan_path), child_context_ref=str(context_path))
            intent_path = child / "simulation_intent.json"
            failure_path = child / "execution_failure.json"
            intent = read_json(intent_path, default={})
            if failure_path.exists() and intent.get("status") != "READ_BACK":
                failures.append(read_json(failure_path))
                continue
            try:
                if intent_path.exists():
                    result = self.agent.recover(child, execution_context=execution_context)
                else:
                    self.agent.gateway.call("set_case", case_key=case_key, run_id=run_id)
                    # The child references the saved plan. Its audit calls have their own
                    # evidence session while Codex still uses the Campaign research ID.
                    self.agent._begin_session(context["research_id"])
                    self.agent.recorder.bind_batch(child)
                    expressions = [item["expression"] for item in group]
                    self.agent._ensure_expressions_uncached(
                        expressions, dataset_id, scope, settings, child, label="production simulation")
                    validation = {expr: plan["validation"][expr] for expr in expressions}
                    used_fields = {field_id for verdict in validation.values()
                                   for field_id in verdict["fields"]}
                    proposal = {
                        "hypothesis": "\n".join(item["research"]["idea"] for item in group),
                        "failure_criterion": "\n".join(item["research"]["failure_criterion"] for item in group),
                        "field_generation_tags": list(dict.fromkeys(item["field_id"] for item in group)),
                    }
                    prereg = {
                        "objective": context["objective"], "dataset_id": dataset_id,
                        "research_type": "REGULAR", "dataset_selection": context["dataset_selection"],
                        "scope": scope, "probe_plan": {"probes": []},
                        "fields": [row for row in context["fields"]
                                   if isinstance(row, dict) and row.get("id") in used_fields],
                        "field_plan_ref": str(plan_path), "field_plan_sha256": digest,
                        "proposal": proposal, "variants": group, "simulation_settings": settings,
                        "validation": validation, "operator_counts": {
                            expr: verdict["operator_count"] for expr, verdict in validation.items()},
                    }
                    runner_variants = [self.agent._production_runner_variant(proposal, item, dataset_id, number)
                                       for number, item in enumerate(group, start=1)]
                    result = self.agent._execute_production(
                        child, prereg, runner_variants, case_key, run_id,
                        execution_context=execution_context)
            except Exception as exc:
                if self.agent.stop_flag and self.agent.stop_flag.exists():
                    stopped = True
                    break
                failure = {"group": index, "batch_dir": str(child), "reason": str(exc)}
                atomic_write_json(failure_path, failure)
                failures.append(failure)
                _emit(execution_context, "regular.field_execution_group_blocked",
                      research_id=context.get("research_id"), dataset_id=dataset_id,
                      batch_ref=str(batch_dir), child_batch_ref=str(child),
                      execution_group=index, exception_type=type(exc).__name__,
                      failure_ref=str(failure_path))
                write_progress(f"{dataset_id} · 回测组 {index} 未完成 · {type(exc).__name__} · {child}")
                continue
            completed.append({"batch_dir": str(child), "alpha_ids": result["alpha_ids"],
                              "candidate_status": result["candidate_status"],
                              "attribution_status": result.get("attribution_status", "complete")})
            _emit(execution_context, "regular.field_execution_group_completed",
                  research_id=context.get("research_id"), dataset_id=dataset_id,
                  batch_ref=str(batch_dir), child_batch_ref=str(child),
                  execution_group=index, alpha_ids=result["alpha_ids"],
                  candidate_status=result["candidate_status"],
                  attribution_status=result.get("attribution_status", "complete"))
        self.agent._begin_session(context["research_id"])
        self.agent.recorder.bind_batch(batch_dir)
        alpha_ids = list(dict.fromkeys(alpha for child in completed for alpha in child["alpha_ids"]))
        status = "complete"
        if stopped:
            status = "user_stopped"
        elif not alpha_ids:
            status = "unresolved"
        elif (plan["field_failures"] or failures
              or len(completed) < (len(plan_variants) + 7) // 8):
            status = "partial"
        attribution_status = ("unresolved" if any(
            child["attribution_status"] != "complete" for child in completed)
            else "complete" if completed else "pending")
        # Child qualification already ran against the case ledger. Use its current
        # contender status, since a later group can revise an earlier pairwise result.
        contenders = self.agent._read_case(context["objective"], scope).get("contenders", {})
        candidate_status = ("CANDIDATE" if any(
            contenders.get(alpha_id, {}).get("status") == "CANDIDATE"
            for alpha_id in alpha_ids) else "PENDING_FULL_GATE")
        result = {"dataset_id": dataset_id, "scope": scope, "batch_dir": str(batch_dir),
                  "alpha_ids": alpha_ids, "candidate_status": candidate_status,
                  "field_research_status": status, "field_failures": plan["field_failures"],
                  "attribution_status": attribution_status,
                  "execution_failures": failures, "child_batches": completed,
                  "field_plan_ref": str(plan_path)}
        atomic_write_json(batch_dir / "results.json", result)
        _emit(execution_context, "regular.field_plan_execution_completed",
              research_id=context.get("research_id"), dataset_id=dataset_id,
              batch_ref=str(batch_dir), field_plan_ref=str(plan_path),
              results_ref=str(batch_dir / "results.json"),
              status=status, child_batch_count=len(completed),
              failure_count=len(failures), alpha_ids=alpha_ids,
              child_batch_refs=[row["batch_dir"] for row in completed])
        return result

