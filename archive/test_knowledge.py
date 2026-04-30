import os

# 1. Setup the paths
folder_name = "spotbot_knowledge"
file_name = "portland_harbour.txt"
path = os.path.join(folder_name, file_name)

print(f"--- 📂 SYSTEM CHECK ---")
print(f"Looking for: {path}")
print(f"Full Path: {os.path.abspath(path)}\n")

# 2. Check if the folder even exists
if not os.path.exists(folder_name):
    print(f"❌ ERROR: The folder '{folder_name}' does not exist.")
    print("Check your spelling or create the folder in your SpotBot directory.")

# 3. Check if the file exists
elif not os.path.exists(path):
    print(f"❌ ERROR: File '{file_name}' not found inside '{folder_name}'.")
    print(f"Files actually in there: {os.listdir(folder_name)}")

# 4. If all good, read the file
else:
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
            print("✅ SUCCESS! Knowledge file linked.")
            print("-" * 30)
            print("FILE PREVIEW:")
            print(content[:300]) # Shows the first 300 characters
            print("-" * 30)
    except Exception as e:
        print(f"❌ ERROR: Could not read the file. {e}")