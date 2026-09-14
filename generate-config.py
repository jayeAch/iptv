import json
import subprocess
import os
from exclude_list import GLOBAL_EXCLUDE_LIST

# Load the template file
try:
    with open('config.template.json', 'r') as f:
        template_content = f.read()
except FileNotFoundError:
    print("Error: config.template.json file not found. Please create it.")
    exit(1)

# Replace the placeholder with the actual JSON-formatted list
# json.dumps() correctly formats the list as a JSON string
final_content = template_content.replace('{{GLOBAL_EXCLUDE_LIST}}',json.dumps(GLOBAL_EXCLUDE_LIST, separators=(',',':'), ensure_ascii=False))

# Write the final config file that the app will use
with open('config.tmp.json', 'w') as f:
    f.write(final_content)

# Use jq to format the entire file
try:
    with open('config.json', 'w') as output_file:
        subprocess.run(
            ['jq', '-c', '.', 'config.tmp.json'],  # Format the entire JSON file
            check=True,
            stdout=output_file
        )
    # Clean up temp file
    os.remove('config.tmp.json')
    print("Success: config.json file generated!")
except subprocess.CalledProcessError as e:
    print(f"Error: jq processing failed: {e}")
except FileNotFoundError:
    print("Error: jq command not found. Please install jq.")
