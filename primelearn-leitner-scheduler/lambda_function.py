import json
import boto3
from datetime import datetime, timedelta
from decimal import Decimal
from boto3.dynamodb.conditions import Key

dynamodb = boto3.resource('dynamodb', region_name='ap-south-1')
LEITNER_BOX_TABLE = "LeitnerBox"

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

def handle_get_due(event):
    learner_id = (event.get('queryStringParameters') or {}).get('learner_id')
    
    if not learner_id:
        return respond(400, {"error": "learner_id is required"})
        
    table = dynamodb.Table(LEITNER_BOX_TABLE)
    now = datetime.utcnow().isoformat()
    
    try:
        response = table.query(
            KeyConditionExpression=Key('learner_id').eq(learner_id)
        )
        items = response.get('Items', [])
        
        due_concepts = []
        for item in items:
            if item.get('next_review_date', now) <= now:
                due_concepts.append(item)
                
        # Sort due_concepts in ascending order of next_review_date
        return respond(200, {
            "learner_id": learner_id,
            "due": sorted(due_concepts, key=lambda x: x.get('next_review_date', ''))
        })

    except Exception as e:
        return respond(500, {"error": "Failed to query due concepts", "details": str(e)})

def handle_post_due(event):
    body = get_body(event)
    learner_id = body.get('learner_id')
    concept_id = body.get('concept_id')
    correct = body.get('correct')
    
    if learner_id is None or concept_id is None or correct is None:
        return respond(400, {"error": "learner_id, concept_id, and correct are required"})
        
    table = dynamodb.Table(LEITNER_BOX_TABLE)
    
    # 1. Read current box_number
    response = table.get_item(Key={'learner_id': learner_id, 'concept_id': concept_id})
    item = response.get('Item', {})
    
    box_number = int(item.get('box_number', 1))
    
    # 2. Adjust box number
    if correct:
        box_number = min(5, box_number + 1)
    else:
        box_number = 1
        
    # 3. Calculate next_review_date
    intervals = {
        1: 1,
        2: 3,
        3: 7,
        4: 14,
        5: 30
    }
    
    days_to_add = intervals.get(box_number, 1)
    now_dt = datetime.utcnow()
    next_review_dt = now_dt + timedelta(days=days_to_add)
    
    now_iso = now_dt.isoformat()
    next_review_iso = next_review_dt.isoformat()
    
    # 4. Save to LeitnerBox table
    table.put_item(Item={
        'learner_id': learner_id,
        'concept_id': concept_id,
        'box_number': Decimal(str(box_number)),
        'next_review_date': next_review_iso,
        'last_reviewed': now_iso
    })
    
    return respond(200, {
        "learner_id": learner_id,
        "concept_id": concept_id,
        "box_number": box_number,
        "next_review_date": next_review_iso,
        "last_reviewed": now_iso
    })


def lambda_handler(event, context):
    try:
        path = event.get('resource') or event.get('rawPath', '')
        http_method = event.get('httpMethod') or event.get('requestContext', {}).get('http', {}).get('method', '')
        
        if http_method == 'GET' and path.endswith('/leitner/due'):
            return handle_get_due(event)
        elif http_method == 'POST' and path.endswith('/leitner/due'):
            return handle_post_due(event)
            
        return respond(404, {"error": f"Route not found: {http_method} {path}"})
    except Exception as e:
        print(f"Error: {str(e)}")
        return respond(500, {"error": "Internal server error", "details": str(e)})
