import json
import uuid
import boto3
from datetime import datetime
from decimal import Decimal

# AWS Clients
dynamodb = boto3.resource('dynamodb', region_name='ap-south-1')
bedrock = boto3.client('bedrock-runtime', region_name='ap-south-1')

# Constants
LEARNER_STATE_TABLE = "LearnerState"
ASSESSMENTS_TABLE = "Assessments"
KNOWLEDGE_GRAPH_TABLE = "KnowledgeGraph"
HAIKU_MODEL_ID = "anthropic.claude-haiku-4-5"

def respond(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body)
    }

def get_body(event):
    if not event.get('body'):
        return {}
    return json.loads(event['body']) if isinstance(event['body'], str) else event['body']

def handle_register(event):
    body = get_body(event)
    name = body.get('name', 'Anonymous')
    email = body.get('email', '')
    language = body.get('language', 'en')
    
    learner_id = str(uuid.uuid4())
    table = dynamodb.Table(LEARNER_STATE_TABLE)
    table.put_item(Item={
        'learner_id': learner_id,
        'name': name,
        'email': email,
        'language': language,
        'ability_score': Decimal('0.0'),
        'streak': 0,
        'onboarding_complete': False,
        'created_at': datetime.utcnow().isoformat(),
        'last_active': datetime.utcnow().isoformat(),
        'goals': []
    })
    
    return respond(201, {"message": "User registered successfully", "learner_id": learner_id})

def handle_goal(event):
    body = get_body(event)
    learner_id = body.get('learner_id')
    goal = body.get('goal')
    
    if not learner_id or not goal:
        return respond(400, {"error": "learner_id and goal are required"})
        
    table = dynamodb.Table(LEARNER_STATE_TABLE)
    table.update_item(
        Key={'learner_id': learner_id},
        UpdateExpression="SET goals = list_append(if_not_exists(goals, :empty_list), :new_goal)",
        ExpressionAttributeValues={
            ':new_goal': [goal],
            ':empty_list': []
        }
    )
    
    return respond(200, {"message": "Goal updated successfully", "learner_id": learner_id})

def handle_get_assessment(event):
    learner_id = event.get('queryStringParameters', {}).get('learner_id')
    if not learner_id:
        return respond(400, {"error": "learner_id is required"})
        
    table = dynamodb.Table(LEARNER_STATE_TABLE)
    response = table.get_item(Key={'learner_id': learner_id})
    user_data = response.get('Item', {})
    goals = user_data.get('goals', [])
    
    prompt = f"Generate 6 multiple-choice assessment questions for a learner aiming to learn: {', '.join(goals) if goals else 'General coding'}. Vary the difficulty levels across these exactly: 0.2, 0.4, 0.6, 0.7, 0.8, 0.9. Return as a JSON array of objects with 'question', 'options' (array of 4 strings), 'correct_index' (0-3), and 'difficulty' (the float value)."
    
    bedrock_body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 1024,
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
    try:
        content = response_body['content'][0]['text']
        content = content.replace("```json", "").replace("```", "").strip()
        # Extract json array from content
        questions = json.loads(content[content.find('['):content.rfind(']')+1])
    except Exception as e:
        questions = [
            {"question": "What is Python?", "options": ["A snake", "A language", "A car", "A planet"], "correct_index": 1, "difficulty": 0.2},
            {"question": "What is a variable?", "options": ["A container", "A animal", "A star", "A food"], "correct_index": 0, "difficulty": 0.4},
            {"question": "What does print() do?", "options": ["Nothing", "Plays sound", "Prints text", "Closes app"], "correct_index": 2, "difficulty": 0.6},
            {"question": "What is a list?", "options": ["A number", "A boolean", "A string", "A collection"], "correct_index": 3, "difficulty": 0.7},
            {"question": "What is a dict?", "options": ["Key-value", "Just keys", "Just values", "Nothing"], "correct_index": 0, "difficulty": 0.8},
            {"question": "What is a class?", "options": ["A function", "A template", "A variable", "A loop"], "correct_index": 1, "difficulty": 0.9}
        ]
        
    season_id = str(uuid.uuid4())
    assessments_table = dynamodb.Table(ASSESSMENTS_TABLE)
    assessments_table.put_item(Item={
        'learner_id': learner_id,
        'season_id': season_id,
        'questions': questions,
        'created_at': datetime.utcnow().isoformat(),
        'status': 'PENDING'
    })
        
    return respond(200, {"season_id": season_id, "questions": questions})

def handle_assessment_answer(event):
    body = get_body(event)
    learner_id = body.get('learner_id')
    season_id = body.get('season_id')
    answers = body.get('answers') # list of indices
    
    if not all([learner_id, season_id, answers]):
        return respond(400, {"error": "learner_id, season_id, and answers required"})
        
    table = dynamodb.Table(ASSESSMENTS_TABLE)
    response = table.get_item(Key={'learner_id': learner_id, 'season_id': season_id})
    assessment = response.get('Item')
    
    if not assessment:
        return respond(404, {"error": "Assessment not found"})
        
    questions = assessment.get('questions', [])
    score = 0
    sum_correct_diff = 0.0
    sum_all_diff = 0.0
    
    for idx, ans in enumerate(answers):
        if idx < len(questions):
            diff = float(questions[idx].get('difficulty', 0.5))
            sum_all_diff += diff
            if questions[idx]['correct_index'] == ans:
                score += 1
                sum_correct_diff += diff
                
    ability_score = float(sum_correct_diff / sum_all_diff) if sum_all_diff > 0 else 0.0
            
    table.update_item(
        Key={'learner_id': learner_id, 'season_id': season_id},
        UpdateExpression="SET #s = :status, score = :score",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":status": "COMPLETED", ":score": score}
    )
    
    # Update LearnerState
    learner_table = dynamodb.Table(LEARNER_STATE_TABLE)
    language = body.get('language', 'en')
    
    # Needs to store ability as a string or Decimal for DynamoDB to accept float
    from decimal import Decimal
    learner_table.update_item(
        Key={'learner_id': learner_id},
        UpdateExpression="SET ability_score = :ability, onboarding_complete = :oc, #lang = :lang",
        ExpressionAttributeNames={"#lang": "language"},
        ExpressionAttributeValues={
            ":ability": Decimal(str(ability_score)),
            ":oc": True,
            ":lang": language
        }
    )
    
    # Read KnowledgeGraph table to unlock concepts based on ability_score
    kg_table = dynamodb.Table(KNOWLEDGE_GRAPH_TABLE)
    
    # Realistically, evaluating 'root' vs 'advanced' would mean scanning or querying a GSI.
    # For constraints and demonstration, we assume all items can be scanned (small table initially)
    # or querying by an index like 'node_level' if it existed.
    # Here we perform a mock filtering since real schema attributes like `is_root` aren't detailed.
    response = kg_table.scan()
    concepts = response.get('Items', [])
    
    unlocked_concepts = []
    for concept in concepts:
        # Assuming concept items have a 'level' or similar attribute.
        # Fallback to unlocking all root concepts if no attributes
        level = concept.get('level', 'root')
        if ability_score < 0.4 and level == 'root':
            unlocked_concepts.append(concept['concept_id'])
        elif 0.4 <= ability_score <= 0.7 and level in ['root', 'intermediate']:
            unlocked_concepts.append(concept['concept_id'])
        elif ability_score > 0.7:
            unlocked_concepts.append(concept['concept_id'])
    
    return respond(200, {
        "message": "Assessment graded", 
        "score": score, 
        "total": len(questions),
        "ability_score": ability_score,
        "unlocked_concepts": unlocked_concepts
    })


def lambda_handler(event, context):
    try:
        path = event.get('resource') or event.get('rawPath', '')
        http_method = event.get('httpMethod') or event.get('requestContext', {}).get('http', {}).get('method', '')
        
        if http_method == 'POST' and path.endswith('/auth/register'):
            return handle_register(event)
        elif http_method == 'POST' and path.endswith('/onboarding/goal'):
            return handle_goal(event)
        elif http_method == 'GET' and '/onboarding/assessment' in path and not path.endswith('/answer'):
            return handle_get_assessment(event)
        elif http_method == 'POST' and path.endswith('/onboarding/assessment/answer'):
            return handle_assessment_answer(event)
            
        return respond(404, {"error": f"Route not found: {http_method} {path}"})
    except Exception as e:
        print(f"Error: {str(e)}")
        return respond(500, {"error": "Internal server error", "details": str(e)})
