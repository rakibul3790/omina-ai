import json
import numpy as np
import random
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

# 1. Load data from the JSON file
# We are telling Python to open and read our memory file
with open('intents.json', 'r', encoding='utf-8') as file:
    data = json.load(file)

patterns = []
tags = []
responses_dict = {}

# 2. Extract data to train the AI
# Here we separate the questions (patterns) and the answers (responses)
for intent in data['intents']:
    responses_dict[intent['tag']] = intent['responses']
    for pattern in intent['patterns']:
        patterns.append(pattern)
        tags.append(intent['tag'])

# 3. Convert text to numbers (Vectorization)
# AI only understands numbers, so we convert text into mathematical values
vectorizer = TfidfVectorizer()
X = vectorizer.fit_transform(patterns)

# 4. Define the AI's thinking process
def get_response(user_input):
    # Convert the user's input into numbers
    user_vec = vectorizer.transform([user_input])
    
    # Check mathematically how similar the input is to our learned patterns
    similarities = cosine_similarity(user_vec, X)
    
    # Find the best matching pattern
    best_match_idx = np.argmax(similarities)
    highest_similarity = similarities[0][best_match_idx]
    
    # If the AI is not confident (similarity below 20%)
    if highest_similarity < 0.2:
        return "আমি বুঝতে পারিনি। আমাকে JSON ফাইলে নতুন কিছু শেখান! / I don't understand yet."
    
    # Get the correct category (tag) and return a random response from it
    matched_tag = tags[best_match_idx]
    return random.choice(responses_dict[matched_tag])

# 5. Start the chat interface
print("AI is ready! (Type 'quit' or 'exit' to stop)")
print("-" * 40)

while True:
    user_text = input("You: ")
    if user_text.lower() in ['quit', 'exit']:
        print("AI: Goodbye! / বিদায়!")
        break
    
    reply = get_response(user_text)
    print(f"AI: {reply}")