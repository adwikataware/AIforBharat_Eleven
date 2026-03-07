import json
import boto3
import os
from datetime import datetime
from decimal import Decimal

dynamodb = boto3.resource('dynamodb', region_name='ap-south-1')
bedrock = boto3.client('bedrock-runtime', region_name='ap-south-1')

SESSION_LOGS_TABLE = "SessionLogs"
HAIKU_MODEL_ID = os.environ.get("HAIKU_MODEL_ID", "anthropic.claude-haiku-4-5")
GUARDRAIL_ID = os.environ.get("GUARDRAIL_ID", "eaul167bm603")

class DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        return super(DecimalEncoder, self).default(obj)

def respond(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, cls=DecimalEncoder)
    }

def get_body(event):
    if not event.get('body'): return {}
    return json.loads(event['body']) if isinstance(event['body'], str) else event['body']

def handle_mentor_hint(event):
    body = get_body(event)
    
    learner_id = body.get('learner_id')
    concept_id = body.get('concept_id')
    question = body.get('question')
    hint_level = int(body.get('hint_level', 1))
    
    # Optional context session ID
    session_id = body.get('session_id')
    
    if not all([learner_id, concept_id, question]):
        return respond(400, {"error": "learner_id, concept_id, and question are required"})
        
    # Cap hint level to 1-4 safely
    hint_level = max(1, min(4, hint_level))
    
    if hint_level == 1:
        system_prompt = "You are a Socratic tutor. Ask a broad guiding question to nudge the learner. Do NOT give the answer."
    elif hint_level == 2:
        system_prompt = "You are a Socratic tutor. Give a more specific hint that points toward the concept. Do NOT give the answer."
    elif hint_level == 3:
        system_prompt = "You are a Socratic tutor. Give a near-direct hint. You may explain the concept partially but do NOT give the final answer."
    else:
        system_prompt = "You are a helpful tutor. The learner has struggled enough. Give a clear, direct, complete explanation and answer."
        
    user_message = f"Concept: {concept_id}\nLearner Question: {question}"
    
    bedrock_body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 800,
        "system": system_prompt, # Put system prompt directly in the bedrock body
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": user_message}]}
        ]
    }
    
    request_params = {
        "modelId": HAIKU_MODEL_ID,
        "body": json.dumps(bedrock_body),
        "accept": "application/json",
        "contentType": "application/json"
    }
    
    # For L1, L2, L3 — invoke Bedrock WITH Guardrail
    if hint_level < 4:
        request_params["guardrailIdentifier"] = GUARDRAIL_ID
        request_params["guardrailVersion"] = "DRAFT"
        
    try:
        res = bedrock.invoke_model(**request_params)
        response_body = json.loads(res.get('body').read())
        content_text = response_body.get('content', [{}])[0].get('text', "I'm having trouble providing a hint right now. Please try again.")
    except Exception as e:
        print(f"Error invoking Bedrock: {e}")
        content_text = "There was an error generating your hint. Please contact support if this persists."
        
    # Log the interaction
    logs_table = dynamodb.Table(SESSION_LOGS_TABLE)
    logs_table.put_item(Item={
        'learner_id': learner_id,
        'timestamp': datetime.utcnow().isoformat(),
        'action': 'MENTOR_HINT',
        'concept_id': concept_id,
        'hint_level': Decimal(str(hint_level)),
        'question': question[:200]
    })
    
    is_direct_answer = (hint_level == 4)
    next_level = (hint_level + 1) if hint_level < 4 else None
    
    return respond(200, {
        "hint": content_text,
        "hint_level": hint_level,
        "is_direct_answer": is_direct_answer,
        "next_level": next_level
    })


def lambda_handler(event, context):
    try:
        path = event.get('resource') or event.get('rawPath', '')
        http_method = event.get('httpMethod') or event.get('requestContext', {}).get('http', {}).get('method', '')
        
        if http_method == 'POST' and path.endswith('/mentor/hint'):
            return handle_mentor_hint(event)
            
        return respond(404, {"error": f"Route not found: {http_method} {path}"})
    except Exception as e:
        print(f"Error: {str(e)}")
        return respond(500, {"error": "Internal server error", "details": str(e)})
