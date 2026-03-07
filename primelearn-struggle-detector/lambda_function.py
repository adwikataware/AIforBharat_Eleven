import json
import boto3
from datetime import datetime
from decimal import Decimal

dynamodb = boto3.resource('dynamodb', region_name='ap-south-1')
bedrock = boto3.client('bedrock-runtime', region_name='ap-south-1')

SESSION_LOGS_TABLE = "SessionLogs"

# Not used, but keeping constraint
HAIKU_MODEL_ID = "anthropic.claude-haiku-4-5"

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

def handle_struggle_signal(event):
    body = get_body(event)
    learner_id = body.get('learner_id')
    concept_id = body.get('concept_id')
    
    error_rate = float(body.get('error_rate', 0.0))
    idle_time_seconds = int(body.get('idle_time_seconds', 0))
    undo_count = int(body.get('undo_count', 0))
    gate_failures = int(body.get('gate_failures', 0))
    
    if not learner_id or not concept_id:
        return respond(400, {"error": "learner_id and concept_id are required"})
        
    # Calculate ZPD Struggle Score
    error_score = min(error_rate * 40.0, 40.0)
    idle_score = min((idle_time_seconds / 300.0) * 25.0, 25.0)
    undo_score = min((undo_count / 10.0) * 20.0, 20.0)
    gate_score = min(gate_failures * 5.0, 15.0)
    
    struggle_score = error_score + idle_score + undo_score + gate_score
    
    # Classify ZPD zone
    if struggle_score < 20:
        zone = "too_easy"
    elif struggle_score <= 75:
        zone = "productive"
    else:
        zone = "too_hard"
        
    # Log the struggle
    logs_table = dynamodb.Table(SESSION_LOGS_TABLE)
    logs_table.put_item(Item={
        'learner_id': learner_id,
        'timestamp': datetime.utcnow().isoformat(),
        'action': 'STRUGGLE_SIGNAL',
        'concept_id': concept_id,
        'struggle_score': Decimal(str(struggle_score)),
        'zone': zone,
        'error_rate': Decimal(str(error_rate)),
        'idle_time_seconds': idle_time_seconds,
        'undo_count': undo_count,
        'gate_failures': gate_failures
    })
    
    return respond(200, {
        "learner_id": learner_id,
        "concept_id": concept_id,
        "struggle_score": struggle_score,
        "zone": zone,
        "trigger_mentor": (zone == "too_hard")
    })

def lambda_handler(event, context):
    try:
        path = event.get('resource') or event.get('rawPath', '')
        http_method = event.get('httpMethod') or event.get('requestContext', {}).get('http', {}).get('method', '')
        
        if http_method == 'POST' and path.endswith('/struggle/signal'):
            return handle_struggle_signal(event)
            
        return respond(404, {"error": f"Route not found: {http_method} {path}"})
    except Exception as e:
        print(f"Error: {str(e)}")
        return respond(500, {"error": "Internal server error", "details": str(e)})
