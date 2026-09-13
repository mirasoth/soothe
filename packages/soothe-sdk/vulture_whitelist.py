"""Vulture whitelist for soothe-sdk protocol method parameters.

These names are interface contract parameters in abstract/protocol methods
whose bodies are ``...`` (ellipsis). Vulture flags them as unused because
the body doesn't reference them, but they define the callable signature.
"""

# ruff: noqa: F821

# core_agent.py — astream / execution_astream / execute_stream parameters
execution_scope
input_arg
stream_mode
subgraphs

# durability.py — DurabilityStore protocol parameters
thread_id
thread_filter

# identity.py — IdentityService protocol parameters
expiry_days
aksk_id
access_key
refresh_token
jti
active_only
channel
sender_id

# memory.py — MemoryStore protocol parameters
limit
item_id

# operation_security.py — OperationSecurity protocol parameters
request

# policy.py — PolicyProtocol parameters
child_name
parent_permissions

# vector_store.py — VectorStore protocol parameters
distance
vector_size
vectors
payloads
vector
filters
record_id

# workspace_sync.py — WorkspaceSync protocol parameters (abstract bodies are `...`)
if_match
artifact_path
content_type
