import google.generativeai as genai

# Initialize the client
client = genai.Client(api_key="AIzaSyAPB-nMEuyqZJxEYzaUt_UPP-0w4UUBWKE")

# Retrieve information about a specific model
model_info = client.models.get(model="gemini-2.0-flash")

# Print the model details
print(model_info)
