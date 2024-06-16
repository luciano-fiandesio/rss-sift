import replicate
import os

# Set the API token
os.environ["REPLICATE_API_TOKEN"] = "r8_Y3YJ78Qs0HLP3t0UX7LgbSi8CSco0QP3uRvgQ"


input = {
    "prompt": """Your task is to determine if a book title is about one of the following topics:
- software development
- machine learning
- ai
- devops
- programming languages
- data science
- networking
- software architecture
- technical leadership

You should only answer with yes or no. Do not output additional code, comments or explanation.

This is the book title: Pirate vs. Pirate: The Terrific Tale of a Big, Blustery Maritime Match""",
"system_prompt":"You are an expert and helpful software developer and data expert"}

output = replicate.run(
    "meta/meta-llama-3-70b-instruct",
    input=input
)
print("".join(output))