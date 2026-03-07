import json
import boto3
import uuid
import os
from datetime import datetime
from decimal import Decimal
from boto3.dynamodb.conditions import Key
import botocore.exceptions

# AWS Clients
dynamodb = boto3.resource('dynamodb', region_name='ap-south-1')
bedrock = boto3.client('bedrock-runtime', region_name='ap-south-1')
s3 = boto3.client('s3', region_name='ap-south-1')

# Constants / Env Vars
LEARNER_STATE_TABLE = "LearnerState"
SESSION_LOGS_TABLE = "SessionLogs"
KNOWLEDGE_GRAPH_TABLE = "KnowledgeGraph"
LEARNER_MASTERY_TABLE = "LearnerMastery"
LEITNER_BOX_TABLE = "LeitnerBox"
HAIKU_MODEL_ID = os.environ.get("HAIKU_MODEL_ID", "anthropic.claude-haiku-4-5")
S3_CONTENT_BUCKET = os.environ.get("S3_CONTENT_BUCKET", "primelearn-content-cache-mumbai")

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

def determine_format(concept, is_revision=False, time_available=30):
    if is_revision or time_available < 10:
        return 'Quick Byte'
        
    c_type = concept.get('type', '').lower()
    requires_hands_on = concept.get('requires_hands_on', False)
    depth = concept.get('depth', '').lower()
    
    if c_type in ['architectural', 'visual', 'networking', 'cloud']:
        return 'Visual Story'
    if requires_hands_on:
        return 'Code Lab'
    if c_type == 'applied':
        return 'Case Study'
    if c_type == 'theoretical' and depth in ['intermediate', 'advanced']:
        return 'Concept X-Ray'
        
    return 'Visual Story'

def handle_get_episode(event):
    path_parameters = event.get('pathParameters') or {}
    episode_id = path_parameters.get('episode_id')
    query_params = event.get('queryStringParameters') or {}
    learner_id = query_params.get('learner_id')
    concept_id = query_params.get('concept_id')
    
    if not all([episode_id, learner_id, concept_id]):
        return respond(400, {"error": "episode_id, learner_id, and concept_id are required"})

    s3_key = f"episodes/{episode_id}.json"
    
    # 4. Check S3 Cache
    try:
        s3_response = s3.get_object(Bucket=S3_CONTENT_BUCKET, Key=s3_key)
        cached_episode = json.loads(s3_response['Body'].read().decode('utf-8'))
        return respond(200, cached_episode)
    except botocore.exceptions.ClientError as e:
        if e.response['Error']['Code'] == 'NoSuchKey':
            pass # cache miss
        else:
            print(f"S3 Error: {e}")

    # 1. Read learner profile  
    state_table = dynamodb.Table(LEARNER_STATE_TABLE)
    learner_resp = state_table.get_item(Key={'learner_id': learner_id})
    learner = learner_resp.get('Item', {})
    
    ability_score = float(learner.get('ability_score', 0.5))
    language = learner.get('language', 'en')
    
    # 2. Read concept metadata
    kg_table = dynamodb.Table(KNOWLEDGE_GRAPH_TABLE)
    concept_resp = kg_table.get_item(Key={'concept_id': concept_id})
    concept = concept_resp.get('Item', {})
    concept_name = concept.get('name', concept_id)
    
    is_revision = query_params.get('is_revision', 'false').lower() == 'true'
    time_available = int(query_params.get('time_available', 30))
    
    # 3. Select format
    format_type = determine_format(concept, is_revision=is_revision, time_available=time_available)
    
    # 5. Generate via Bedrock
    context_str = ""
    if format_type == 'Case Study':
        context_str = " Include an Indian company context in your explanation (e.g., Zomato, UPI, Flipkart, IRCTC)."
        
    prompt = (
        f"Generate a learning episode about '{concept_name}'. "
        f"The format MUST be '{format_type}'. "
        f"The learner has an ability score of {ability_score} (0.0 to 1.0, adapt complexity accordingly) "
        f"and prefers the '{language}' language.{context_str} "
        "Return EXCLUSIVELY a JSON object with keys: "
        "'episode_id', 'title', 'content', 'activities' (an array of objects with 'type', 'question', 'options', 'correct')."
    )
    
    bedrock_body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 1500,
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": prompt}]}
        ]
    }
    
    res = bedrock.invoke_model(
        modelId=HAIKU_MODEL_ID,
        body=json.dumps(bedrock_body),
        accept="application/json",
        contentType="application/json"
    )
    
    response_body = json.loads(res.get('body').read())
    content_text = response_body['content'][0]['text']
    content_text = content_text.replace("```json", "").replace("```", "").strip()
    
    try:
        episode_data = json.loads(content_text[content_text.find('{'):content_text.rfind('}')+1])
        episode_data['episode_id'] = episode_id
        episode_data['format'] = format_type
        # 6. Save to S3 Cache
        s3.put_object(
            Bucket=S3_CONTENT_BUCKET,
            Key=s3_key,
            Body=json.dumps(episode_data).encode('utf-8'),
            ContentType='application/json'
        )
    except Exception as e:
        print(f"Error parsing or saving bedrock response: {e}")
        episode_data = {
            "episode_id": episode_id,
            "title": f"Episode on {concept_name}",
            "content": f"We encountered an error generating this {format_type} episode.",
            "activities": []
        }
            
    return respond(200, episode_data)


def handle_post_progress(event):
    path_parameters = event.get('pathParameters') or {}
    episode_id = path_parameters.get('episode_id')
    body = get_body(event)
    
    learner_id = body.get('learner_id')
    concept_id = body.get('concept_id')
    completion_rate = float(body.get('completion_rate', 0.0))
    time_spent_seconds = int(body.get('time_spent_seconds', 0))
    
    if not all([episode_id, learner_id, concept_id]):
        return respond(400, {"error": "episode_id, learner_id, and concept_id are required"})
        
    table = dynamodb.Table(SESSION_LOGS_TABLE)
    timestamp = datetime.utcnow().isoformat()
    
    action = "EPISODE_COMPLETE" if completion_rate >= 1.0 else "PROGRESS_UPDATE"
    
    table.put_item(Item={
        'learner_id': learner_id,
        'timestamp': timestamp,
        'episode_id': episode_id,
        'concept_id': concept_id,
        'action': action,
        'completion_rate': Decimal(str(completion_rate)),
        'time_spent_seconds': time_spent_seconds
    })
    
    return respond(200, {
        "message": "Progress recorded", 
        "episode_id": episode_id, 
        "completion_rate": completion_rate,
        "action": action
    })


def handle_get_dashboard(event):
    path_parameters = event.get('pathParameters') or {}
    learner_id = path_parameters.get('learner_id')
    
    if not learner_id:
        return respond(400, {"error": "learner_id is required"})
        
    # 1. Read LearnerState
    state_table = dynamodb.Table(LEARNER_STATE_TABLE)
    profile = state_table.get_item(Key={'learner_id': learner_id}).get('Item', {})
    
    # 2. Read LearnerMastery
    mastery_table = dynamodb.Table(LEARNER_MASTERY_TABLE)
    mastery_resp = mastery_table.query(KeyConditionExpression=Key('learner_id').eq(learner_id))
    mastery = mastery_resp.get('Items', [])
    
    # 3. Read LeitnerBox
    leitner_table = dynamodb.Table(LEITNER_BOX_TABLE)
    leitner_resp = leitner_table.query(KeyConditionExpression=Key('learner_id').eq(learner_id))
    leitner = leitner_resp.get('Items', [])
    
    # 4. Read SessionLogs (last 10 entries)
    logs_table = dynamodb.Table(SESSION_LOGS_TABLE)
    logs_resp = logs_table.query(
        KeyConditionExpression=Key('learner_id').eq(learner_id),
        ScanIndexForward=False,
        Limit=10
    )
    recent_activity = logs_resp.get('Items', [])
    
    return respond(200, {
        "learner_id": learner_id,
        "profile": profile,
        "mastery": mastery,
        "leitner_box": leitner,
        "recent_activity": recent_activity
    })


def lambda_handler(event, context):
    try:
        path = event.get('resource') or event.get('rawPath', '')
        http_method = event.get('httpMethod') or event.get('requestContext', {}).get('http', {}).get('method', '')
        
        if http_method == 'GET' and '/episodes/' in path and not path.endswith('/progress'):
            return handle_get_episode(event)
        elif http_method == 'POST' and '/episodes/' in path and path.endswith('/progress'):
            return handle_post_progress(event)
        elif http_method == 'GET' and '/dashboard/' in path:
            return handle_get_dashboard(event)
            
        return respond(404, {"error": f"Route not found: {http_method} {path}"})
    except Exception as e:
        print(f"Error: {str(e)}")
        return respond(500, {"error": "Internal server error", "details": str(e)})
