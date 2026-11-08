import os

# Tests use mocked transports; importing Haystack must not send telemetry or
# create a profile outside the workspace.
os.environ['HAYSTACK_TELEMETRY_ENABLED'] = 'false'
