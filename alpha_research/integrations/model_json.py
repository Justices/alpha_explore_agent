"""Decode one JSON decision, optionally wrapped in a single fenced block."""
import json
import re


def decode_decision(message):
    try:
        return json.loads(message)
    except json.JSONDecodeError:
        blocks = re.findall(r"```(?:json)?\s*(.*?)\s*```", message,
                            flags=re.IGNORECASE | re.DOTALL)
        if len(blocks) != 1:
            raise
        return json.loads(blocks[0])
