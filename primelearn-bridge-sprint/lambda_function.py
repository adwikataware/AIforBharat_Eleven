import json
import boto3
import os
from datetime import datetime
from decimal import Decimal
from boto3.dynamodb.conditions import Key
from collections import deque

# AWS Clients
dynamodb = boto3.resource('dynamodb', region_name='ap-south-1')
bedrock = boto3.client('bedrock-runtime', region_name='ap-south-1')
s3 = boto3.client('s3', region_name='ap-south-1')

# Constants / Env Vars
LEARNER_MASTERY_TABLE = "LearnerMastery"
KNOWLEDGE_GRAPH_TABLE = "KnowledgeGraph"
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

def handle_generate_sprint(event):
    body = get_body(event)
    learner_id = body.get('learner_id')
    target_concept_id = body.get('target_concept_id')
    
    if not learner_id or not target_concept_id:
        return respond(400, {"error": "learner_id and target_concept_id are required"})

    # 1. Read learner's current mastery
    mastery_table = dynamodb.Table(LEARNER_MASTERY_TABLE)
    mastery_resp = mastery_table.query(KeyConditionExpression=Key('learner_id').eq(learner_id))
    mastery_items = mastery_resp.get('Items', [])
    
    mastered_concepts = set()
    for item in mastery_items:
        p_known = float(item.get('p_known', 0))
        if p_known >= 0.85:
            mastered_concepts.add(item.get('concept_id'))

    # 2. Read full KnowledgeGraph
    kg_table = dynamodb.Table(KNOWLEDGE_GRAPH_TABLE)
    kg_resp = kg_table.scan()
    kg_items = kg_resp.get('Items', [])
    
    # Build dictionary for quick lookup: concept_id -> dict
    graph_map = {}
    for item in kg_items:
        graph_map[item['concept_id']] = {
            'name': item.get('name', item['concept_id']),
            'prerequisites': item.get('prerequisites', []),
            'level': item.get('level', 'unknown')
        }
        
    if target_concept_id not in graph_map:
        return respond(404, {"error": f"Target concept {target_concept_id} not found in KnowledgeGraph"})

    # 3. Run BFS from target_concept_id backwards
    queue = deque([target_concept_id])
    visited = set([target_concept_id])
    gaps = set()
    
    if target_concept_id not in mastered_concepts:
        gaps.add(target_concept_id)
        
    while queue:
        current_id = queue.popleft()
        current_node = graph_map.get(current_id, {})
        prereqs = current_node.get('prerequisites', [])
        
        for prereq in prereqs:
            if prereq not in visited:
                visited.add(prereq)
                if prereq not in mastered_concepts:
                    gaps.add(prereq)
                # Keep going up the tree regardless if mastered, depending on graph structure
                # In strict prerequisites, if we miss a prereq, we need its prereqs too
                queue.append(prereq)

    # 4. If no gaps found
    if not gaps:
        return respond(200, {
            "learner_id": learner_id,
            "target_concept_id": target_concept_id,
            "message": "No gaps detected, learner is ready",
            "gaps_found": []
        })

    # 5. Call Bedrock to generate sprint plan
    gap_list = list(gaps)
    gap_names = [graph_map.get(gid, {}).get('name', gid) for gid in gap_list]
    
    prompt = (
        f"Generate a short bridge sprint for these missing concepts: {', '.join(gap_names)}. "
        "Return JSON with key 'sprint' containing an array of objects with "
        "'concept_id' (match exact IDs if possible, or name), 'title', 'estimated_minutes', "
        "and 'priority' (1=highest)."
    )
    
    bedrock_body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 1500,
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": prompt}]}
        ]
    }
    
    try:
        res = bedrock.invoke_model(
            modelId=HAIKU_MODEL_ID,
            body=json.dumps(bedrock_body),
            accept="application/json",
            contentType="application/json"
        )
        
        # Parse output safely stripping markdown
        response_body = json.loads(res.get('body').read())
        content_text = response_body['content'][0]['text']
        content_text = content_text.replace("```json", "").replace("```", "").strip()
        
        sprint_json = json.loads(content_text[content_text.find('{'):content_text.rfind('}')+1])
        sprint_array = sprint_json.get('sprint', [])
        
    except Exception as e:
        print(f"Failed to generate sprint with Bedrock: {e}")
        sprint_array = [{"concept_id": g, "title": f"Learn {g}", "estimated_minutes": 15, "priority": 1} for g in gap_list]

    # 6. Save sprint to S3
    timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    s3_key = f"episodes/bridge-sprint-{learner_id}-{timestamp}.json"
    
    s3_doc = {
        "learner_id": learner_id,
        "target_concept_id": target_concept_id,
        "gaps_found": gap_list,
        "sprint": sprint_array,
        "created_at": datetime.utcnow().isoformat()
    }
    
    try:
        s3.put_object(
            Bucket=S3_CONTENT_BUCKET,
            Key=s3_key,
            Body=json.dumps(s3_doc).encode('utf-8'),
            ContentType='application/json'
        )
    except Exception as e:
        print(f"Failed to save to S3: {e}")

    # 7. Return
    return respond(200, {
        "learner_id": learner_id,
        "target_concept_id": target_concept_id,
        "gaps_found": gap_list,
        "sprint": sprint_array,
        "s3_key": s3_key
    })

def lambda_handler(event, context):
    try:
        path = event.get('resource') or event.get('rawPath', '')
        http_method = event.get('httpMethod') or event.get('requestContext', {}).get('http', {}).get('method', '')
        
        if http_method == 'POST' and path.endswith('/bridge-sprint/generate'):
            return handle_generate_sprint(event)
            
        return respond(404, {"error": f"Route not found: {http_method} {path}"})
    except Exception as e:
        print(f"Error: {str(e)}")
        return respond(500, {"error": "Internal server error", "details": str(e)})
