"""Weekly prompt refresh — called by refresh-prompts.yml workflow."""
import google.generativeai as genai
from src import config
from src.prompt_engine import reset_bank

def refresh():
    # Reset all prompts to unused — simple reset, no Gemini needed for now
    reset_bank()
    print("Prompt bank reset — all 500 prompts marked unused.")

if __name__ == "__main__":
    refresh()
