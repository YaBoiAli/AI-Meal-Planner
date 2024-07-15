import os
import re
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import StateGraph, END
from langchain_core.messages import SystemMessage, HumanMessage
from langchain_core.pydantic_v1 import BaseModel
from tavily import TavilyClient
from typing import TypedDict, List
from langchain_google_genai import ChatGoogleGenerativeAI
from dotenv import load_dotenv
import sys
import io
import gradio as gr

load_dotenv()

memory = SqliteSaver.from_conn_string(":memory:")

class AgentState(TypedDict):
    task: str
    plan: str
    draft: str
    critique: str
    content: List[str]
    revision_number: int
    max_revisions: int

model = ChatGoogleGenerativeAI(model="gemini-1.5-pro", temperature=0.4)

PLAN_PROMPT = """You are an expert meal outline planner tasked with creating a comprehensive meal plan outline. 
Your task is to give a detailed outline of the meal plan, which includes calories, recipes based on user preferences, a shopping list based on ingredients, and any relevant notes or instructions for the recipes. Ensure the meal plan is balanced, nutritious, and adheres to any dietary restrictions provided. 
Provide specific details and be precise in your outline."""

WRITER_PROMPT = """You are an excellent meal planner generator tasked with crafting a detailed and precise final meal plan with schedules. 
Follow this template: 

- Breakfast -
- Lunch -
- Dinner -
- Optional Snacks -

For each meal and snack, include the shopping list, calories, protein content, and ingredients. Ensure the meal plan is nutritious, balanced, and meets the user's dietary needs. Generate the best possible meal plan based on the provided template, ensuring clarity and conciseness in every detail. If the user provides critique, respond with a revised version of your previous attempts, incorporating their feedback.
Use all the information below as needed: 
------
{content}
"""

REFLECTION_PROMPT = """You are a critic reviewing a meal plan. 
Your task is to generate a detailed critique and provide recommendations for improving the user's meal plan. Ensure that the meal plan meets nutritional requirements and dietary restrictions. Evaluate the recipes based on calories and protein content to ensure they align with the user's needs. Provide constructive feedback and suggest alternative recipes or adjustments as necessary.
"""

RESEARCH_PLAN_PROMPT = """You are a researcher tasked with gathering information to aid in writing a comprehensive meal plan according to the user's meal plan outline. 
Generate a list of search queries to gather relevant information regarding calories, protein content, ingredients, and recipes. Ensure the queries are precise and focused to provide the most useful information. Generate a maximum of 3 queries.
"""

RESEARCH_CRITIQUE_PROMPT = """You are a researcher charged with providing information to support any requested revisions to a meal plan. 
Generate a list of search queries to gather relevant information regarding calories, protein content, ingredients, and alternative recipes. Ensure the queries are specific and targeted to address the user's feedback. Generate a maximum of 3 queries.
"""

class Queries(BaseModel):
    queries: List[str]

tavily = TavilyClient(api_key=os.environ["TAVILY_API_KEY"])


def plan_node(state: AgentState):
    messages = [
        SystemMessage(content=PLAN_PROMPT),
        HumanMessage(content=state['task'])
    ]
    response = model.invoke(messages)
    plan_node_result = print("Plan agent Response: ", response.content)
    plan_node_result
    return {"plan": response.content}

def parse_queries(response_content):
    # Extract queries from the response content using regex
    pattern = r'\*\*"(.*?)"\*\*'
    queries = re.findall(pattern, response_content)
    return queries

def research_meal_plan_node(state: AgentState):
    messages = [
        SystemMessage(content=RESEARCH_PLAN_PROMPT),
        HumanMessage(content=state['task'])
    ]
    response = model.invoke(messages)
    queries = parse_queries(response.content)
    content = state['content'] or []
    for q in queries:
        search_response = tavily.search(query=q, max_results=2)
        for r in search_response['results']:
            content.append(r['content'])
    research_meal_plan_node = print("Research Meal Plan Response:", response.content)  # Debug print
    research_meal_plan_node
    return {"content": content}

def generation_node(state: AgentState):
    content = "\n\n".join(state['content'] or [])
    user_message = HumanMessage(
        content=f"{state['task']}\n\nHere is my meal plan:\n\n{state['plan']}")
    messages = [
        SystemMessage(
            content=WRITER_PROMPT.format(content=content)
        ),
        user_message
    ]
    response = model.invoke(messages)
    generation_node = print("Generation Response: ", response.content)
    generation_node
    return {
        "draft": response.content,
        "revision_number": state.get("revision_number", 1) + 1
    }

def reflection_node(state: AgentState):
    messages = [
        SystemMessage(content=REFLECTION_PROMPT),
        HumanMessage(content=state['draft'])
    ]
    response = model.invoke(messages)
    reflection_node = print("Reflection Response:", response.content)
    reflection_node
    return {"critique": response.content}

def research_critique_node(state: AgentState):
    messages = [
        SystemMessage(content=RESEARCH_CRITIQUE_PROMPT),
        HumanMessage(content=state['critique'])
    ]
    response = model.invoke(messages)
    research_critique_node = print("Research Critique Response:", response.content)  # Debug print
    research_critique_node
    queries = parse_queries(response.content)
    content = state['content'] or []
    for q in queries:
        search_response = tavily.search(query=q, max_results=2)
        for r in search_response['results']:
            content.append(r['content'])
    return {"content": content}

def should_continue(state):
    if state["revision_number"] > state["max_revisions"]:
        return END
    return "reflect_plan"

builder = StateGraph(AgentState)

builder.add_node("meal_planner", plan_node)
builder.add_node("generate", generation_node)
builder.add_node("reflect_plan", reflection_node)
builder.add_node("research_meal_plan", research_meal_plan_node)
builder.add_node("research_critique", research_critique_node)

builder.set_entry_point("meal_planner")

builder.add_conditional_edges(
    "generate", 
    should_continue, 
    {END: END, "reflect_plan": "reflect_plan"}
)

builder.add_edge("meal_planner", "research_meal_plan")
builder.add_edge("research_meal_plan", "generate")
builder.add_edge("reflect_plan", "research_critique")
builder.add_edge("research_critique", "generate")

graph = builder.compile(checkpointer=memory)

def start_agents(task, max_revisions):
    # Save the current stdout
    old_stdout = sys.stdout
    sys.stdout = io.StringIO()  # Redirect stdout to a buffer
    
    try:
        thread = {"configurable": {"thread_id": "1"}}
        responses = list(graph.stream({
            'task': task,
            "max_revisions": max_revisions,
            "revision_number": 1
        }, thread))
    finally:
        # Restore the original stdout
        sys.stdout = old_stdout

    if responses:
        draft = responses[-1].get('generate', {}).get('draft', 'No draft found')
        return draft
    else:
        return "No responses received"

interface = gr.Interface(
    fn = start_agents,
    inputs= [
        gr.Textbox(lines=2, placeholder="Enter your meal planning task..."),
        gr.Slider(1, 3, step=1, label="Max Revisions")
    ],
    outputs="text",
    title="AI Meal Planner",
    description="Generate meal plans based on your dietary requirements and preferences."
)

interface.launch()