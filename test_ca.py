import json

def _submission_to_client_shape(sub: dict, project: dict = None) -> dict:
    DEFAULT_FIELD_VISIBILITY = {
        "custom_answers": True
    }
    
    if sub.get("client_package_snapshot"):
        snap = sub["client_package_snapshot"]
        if snap.get("custom_answers") or not (sub.get("form_data") or {}).get("custom_answers"):
            return snap
        
        fd_for_resolve = sub.get("form_data") or {}
        fv_for_resolve = {**DEFAULT_FIELD_VISIBILITY, **(sub.get("field_visibility") or {})}
        raw_answers_snap = fd_for_resolve.get("custom_answers") or {}
        
        if isinstance(raw_answers_snap, dict) and raw_answers_snap and fv_for_resolve.get("custom_answers"):
            q_text_by_id_snap = {}
            project_cqs_snap = (project or {}).get("custom_questions") or []
            for cq in project_cqs_snap:
                qid = cq.get("id") or ""
                qtext = (cq.get("question") or "").strip()
                if qid and qtext:
                    q_text_by_id_snap[qid] = qtext
                    
            ordered_ids_snap = (
                [cq.get("id") for cq in project_cqs_snap if cq.get("id")]
                if project_cqs_snap else list(raw_answers_snap.keys())
            )
            ca_vis_snap = fv_for_resolve.get("custom_answers")
            filtered_snap = []
            seen_snap = set()
            
            for q_id in ordered_ids_snap:
                if q_id not in raw_answers_snap:
                    continue
                if isinstance(ca_vis_snap, dict) and not ca_vis_snap.get(q_id):
                    continue
                ans = str(raw_answers_snap[q_id] or "").strip()
                if ans:
                    filtered_snap.append({"question": q_text_by_id_snap.get(q_id) or q_id, "answer": ans})
                seen_snap.add(q_id)
                
            for q_id, a in raw_answers_snap.items():
                if q_id in seen_snap:
                    continue
                if isinstance(ca_vis_snap, dict) and not ca_vis_snap.get(q_id):
                    continue
                ans = str(a or "").strip()
                if ans:
                    filtered_snap.append({"question": q_text_by_id_snap.get(q_id) or q_id, "answer": ans})
                    
            if filtered_snap:
                return {**snap, "custom_answers": filtered_snap}
        return snap
    return {}

sub = {
    "client_package_snapshot": {"id": "123", "name": "John Doe"},
    "form_data": {
        "custom_answers": {"q1": "No", "q2": "Yes"}
    },
    "field_visibility": {"custom_answers": True}
}

project = {
    "custom_questions": [
        {"id": "q1", "question": "Can you drive?"},
        {"id": "q2", "question": "Can you swim?"}
    ]
}

shape = _submission_to_client_shape(sub, project=project)
print(json.dumps(shape, indent=2))
