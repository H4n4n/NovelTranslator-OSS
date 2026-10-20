import os

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

api_key = os.getenv("DEEPSEEK_API_KEY")

if not api_key:
    raise RuntimeError("DEEPSEEK_API_KEY was not found in the .env file.")

client = OpenAI(
    api_key=api_key,
    base_url="https://api.deepseek.com",
)

response = client.chat.completions.create(
    model="deepseek-v4-pro",
    messages=[
        {
            "role": "system",
            "content": (
                "You are a professional Chinese-to-English literary translator."
            ),
        },
        {
            "role": "user",
            "content": "Translate this into natural English: 天色渐渐暗了下来。",
        },
    ],
    max_tokens=500,
    extra_body={
        "thinking": {
            "type": "disabled"
        }
    },
)

print("TRANSLATION:")
print(response.choices[0].message.content)

print("\nTOKEN USAGE:")
print(response.usage)