import json
import io
import sys
import contextlib
import traceback

def respond(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body)
    }

def get_body(event):
    if not event.get('body'): return {}
    return json.loads(event['body']) if isinstance(event['body'], str) else event['body']

def handle_execute(event):
    body = get_body(event)
    code = body.get('code')
    language = body.get('language', 'python').lower()
    
    if language != 'python':
        return respond(400, {"error": "Only Python is supported currently"})
    
    if not code:
        return respond(400, {"error": "code is required"})
        
    output_buffer = io.StringIO()
    error_output = None
    success = False
    
    # IMPORTANT: In a real-world scenario, this is highly insecure. 
    # Python code should be executed within an isolated Docker sandbox.
    # For constraints and demonstration, we execute it carefully by capturing stdout.
    
    with contextlib.redirect_stdout(output_buffer):
        with contextlib.redirect_stderr(output_buffer):
            try:
                # Provide a limited globals dictionary
                safe_builtins = {
                    'print': print,
                    'range': range,
                    'len': len,
                    'int': int,
                    'float': float,
                    'str': str,
                    'bool': bool,
                    'list': list,
                    'dict': dict,
                    'set': set,
                    'tuple': tuple,
                    'enumerate': enumerate,
                    'zip': zip,
                    'map': map,
                    'filter': filter,
                    'sorted': sorted,
                    'reversed': reversed,
                    'sum': sum,
                    'min': min,
                    'max': max,
                    'abs': abs,
                    'round': round,
                    'isinstance': isinstance,
                    'type': type,
                }
                
                safe_globals = {
                    "__builtins__": safe_builtins
                }
                exec(code, safe_globals)
                success = True
            except Exception as e:
                error_output = traceback.format_exc()
                print(error_output)

    stdout = output_buffer.getvalue()
    
    if len(stdout) > 10000:
        stdout = stdout[:10000] + "\n... [Output truncated]"
    
    return respond(200, {
        "success": success,
        "output": stdout,
        "error": error_output if not success else None
    })

def lambda_handler(event, context):
    try:
        path = event.get('resource') or event.get('rawPath', '')
        http_method = event.get('httpMethod') or event.get('requestContext', {}).get('http', {}).get('method', '')
        
        if http_method == 'POST' and path.endswith('/code/execute'):
            return handle_execute(event)
            
        return respond(404, {"error": f"Route not found: {http_method} {path}"})
    except Exception as e:
        print(f"Error: {str(e)}")
        return respond(500, {"error": "Internal server error", "details": str(e)})
