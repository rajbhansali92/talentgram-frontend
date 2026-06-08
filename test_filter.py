from backend.core import _filter_talent_for_client
talent = {
  "id": "123",
  "name": "John Doe",
  "custom_answers": [
    {
      "question": "Can you drive?",
      "answer": "No"
    }
  ]
}
vis = {}
out = _filter_talent_for_client(talent, vis)
import json
print(json.dumps(out))
