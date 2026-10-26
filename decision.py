"""Parse the model's final action; do not infer it from its explanation."""
import re
import json


def unique_keys(pairs):
    result = {}
    for key, value in pairs:
        key = key.upper()
        if key in result:
            raise ValueError('duplicate decision key')
        result[key] = value
    return result

def parse_decision(text, n_options):
    """Exact protocol lines, including an invalid last action, never substring PAY."""
    if not isinstance(text, str):
        return {"action": "parse_error", "error_detail": "response is not text"}
    candidate = text.strip()
    fenced = re.fullmatch(r'```(?:json)?\s*\n(.*?)\n```', candidate, re.S | re.I)
    if fenced:
        candidate = fenced[1].strip()
    if candidate.startswith('{'):
        try:
            data = json.loads(candidate, object_pairs_hook=unique_keys)
        except (ValueError, TypeError):
            return {'action':'parse_error', 'error_detail':'invalid or ambiguous JSON decision'}
        action = data.get('ACTION')
        if not isinstance(action,str) or action.strip().upper() not in {'PAY','DECLINE'}:
            return {'action':'parse_error', 'error_detail':'JSON ACTION must be PAY or DECLINE'}
        action = action.strip().upper()
        if action == 'DECLINE':
            return {'action':'DECLINE','option_index':None,'error_detail':None}
        selected = data.get('OPTION')
        if type(selected) not in {int,str} or not re.fullmatch(r'[1-9][0-9]*',str(selected)) or not 1 <= int(selected) <= n_options:
            return {'action':'parse_error', 'error_detail':'invalid JSON OPTION'}
        return {'action':'PAY','option_index':int(selected)-1,'error_detail':None}
    # Join only explicitly labeled fields wrapped across one newline, never Reason.
    candidate = re.sub(r"^([ \t]*(?:ACTION|OPTION))[ \t]*\r?\n[ \t]*:",
                       r"\1:", candidate, flags=re.M | re.I)
    candidate = re.sub(r"^([ \t]*(?:ACTION|OPTION)[ \t]*:)[ \t]*\r?\n[ \t]*(?=[^\r\n])",
                       r"\1 ", candidate, flags=re.M | re.I)
    actions = re.findall(r"^[ \t]*ACTION[ \t]*:[ \t]*([^\r\n]*)", candidate, re.M | re.I)
    action = actions[-1].strip().upper() if actions else None
    if action not in ("PAY", "DECLINE"):
        return {"action": "parse_error", "error_detail": "last ACTION must be PAY or DECLINE"}
    if action == "DECLINE":
        return {"action": action, "option_index": None, "error_detail": None}
    options = re.findall(r"^[ \t]*OPTION[ \t]*:[ \t]*([^\r\n]*)", candidate, re.M | re.I)
    if options:
        selected = options[-1].strip()
        if not re.fullmatch(r"[1-9][0-9]*", selected) or not 1 <= int(selected) <= n_options:
            return {"action": "parse_error", "error_detail": "invalid OPTION"}
        idx = int(selected) - 1
    elif n_options == 1:
        idx = 0
    else:
        return {"action": "parse_error", "error_detail": "multi-option PAY requires OPTION"}
    return {"action": action, "option_index": idx, "error_detail": None}
