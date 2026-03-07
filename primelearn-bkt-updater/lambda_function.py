import json
import boto3
from decimal import Decimal

class DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        return super().default(obj)

dynamodb = boto3.resource('dynamodb', region_name='ap-south-1')
LEARNER_MASTERY_TABLE = "LearnerMastery"

def respond(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, cls=DecimalEncoder)
    }

def get_body(event):
    if not event.get('body'): return {}
    return json.loads(event['body']) if isinstance(event['body'], str) else event['body']

def handle_bkt_update(event):
    """
    Standard Bayesian Knowledge Tracing requires Prior, Likelihood of Guess, Likelihood of Slip, and Transition.
    For simplicity in this Lambda, we apply a simplified probability update for learner mastery.
    """
    body = get_body(event)
    learner_id = body.get('learner_id')
    concept_id = body.get('concept_id')
    is_correct = body.get('is_correct')
    
    if learner_id is None or concept_id is None or is_correct is None:
        return respond(400, {"error": "learner_id, concept_id, and is_correct are required"})
        
    table = dynamodb.Table(LEARNER_MASTERY_TABLE)
    
    # Get current mastery
    response = table.get_item(Key={'learner_id': learner_id, 'concept_id': concept_id})
    item = response.get('Item', {})
    
    p_known = float(item.get('p_known', 0.1))  # Default 0.1 prior
    
    # BKT Constants (can be fetched from a Concept Metadata table realistically)
    p_slip = 0.1
    p_guess = 0.2
    p_transit = 0.3
    
    if is_correct:
        # P(L | Correct) = P(Correct | L) * P(L) / P(Correct)
        # P(Correct) = P(Correct | L)*P(L) + P(Correct | ~L)*P(~L) -> (1-slip)*p_known + guess*(1-p_known)
        p_known_given_obs = ((1 - p_slip) * p_known) / (((1 - p_slip) * p_known) + (p_guess * (1 - p_known)))
    else:
        # P(L | Incorrect) = P(Incorrect | L) * P(L) / P(Incorrect)
        # P(Incorrect) = P(Incorrect | L)*P(L) + P(Incorrect | ~L)*P(~L) -> slip*p_known + (1-guess)*(1-p_known)
        p_known_given_obs = (p_slip * p_known) / ((p_slip * p_known) + ((1 - p_guess) * (1 - p_known)))
        
    # P(L_next) = P(L | Obs) + P(Transit) * P(~L | Obs)
    p_known_next = p_known_given_obs + p_transit * (1 - p_known_given_obs)
    
    # Save back to DynamoDB
    table.put_item(Item={
        'learner_id': learner_id,
        'concept_id': concept_id,
        'p_known': Decimal(str(round(p_known_next, 4)))
    })
    
    mastery_achieved = p_known_next >= 0.85
    
    return respond(200, {
        "message": "Mastery updated",
        "learner_id": learner_id,
        "concept_id": concept_id,
        "p_known": round(p_known_next, 4),
        "mastery_achieved": mastery_achieved
    })

def lambda_handler(event, context):
    try:
        path = event.get('resource') or event.get('rawPath', '')
        http_method = event.get('httpMethod') or event.get('requestContext', {}).get('http', {}).get('method', '')
        
        # This function might be triggered mostly via internal requests, but exposing via POST /bkt/update for completeness
        if http_method == 'POST' and path.endswith('/bkt/update'):
            return handle_bkt_update(event)
            
        return respond(404, {"error": f"Route not found: {http_method} {path}"})
    except Exception as e:
        print(f"Error: {str(e)}")
        return respond(500, {"error": "Internal server error", "details": str(e)})
