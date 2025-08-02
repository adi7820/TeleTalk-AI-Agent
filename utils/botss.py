#
# Copyright (c) 2024–2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

import argparse
import asyncio
import os
from contextlib import asynccontextmanager
from typing import Dict
from time import time
import uvicorn
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI
from fastapi.responses import RedirectResponse
from loguru import logger
from pipecat_ai_small_webrtc_prebuilt.frontend import SmallWebRTCPrebuiltUI
from datetime import datetime
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
from pipecat.services.llm_service import FunctionCallParams
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.network.small_webrtc import SmallWebRTCTransport
from pipecat.transports.network.webrtc_connection import IceServer, SmallWebRTCConnection
from pipecat.services.aws.stt import AWSTranscribeSTTService
from pipecat.services.aws.tts import AWSPollyTTSService
from pipecat.services.aws.llm import AWSBedrockLLMService
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from retrieval import search_health_info, summarize_search_results
from pipecat.utils.text.markdown_text_filter import MarkdownTextFilter
from typing import TypedDict, Union, List
from pipecat_flows import (
    FlowArgs,
    FlowManager,
    FlowsFunctionSchema,
    NodeConfig,
    FlowResult
)
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
from datetime import date
from dateutil.relativedelta import relativedelta


load_dotenv(override=True)

app = FastAPI()

# Store connections by pc_id
pcs_map: Dict[str, SmallWebRTCConnection] = {}

ice_servers = [
    IceServer(
        urls="stun:stun.l.google.com:19302",
    )
]

# Mount the frontend at /
app.mount("/client", SmallWebRTCPrebuiltUI)

class PaymentData(TypedDict):
    six_monthly_installment: float
    start_date: str
    six_end_date: str
    twelve_monthly_installment: float
    twelve_end_date: str

class CustomerRecord(FlowResult):
    name: str
    due_amount: float
    due_date: str

class SetupPaymentPlan(FlowResult):
    user_response: bool

class TypeofPaymentPlan(FlowResult):
    plan_type: str


class PaymentPlanDetails(FlowResult, PaymentData):
    pass

async def fetch_customer_records_handler(
    args: FlowArgs, flow_manager: FlowManager
) -> tuple[CustomerRecord, NodeConfig]:
    name = args["name"]
    sample: List[CustomerRecord] = [
        {"name": "Henry Smith", "due_amount": 5500.00, "due_date": "2025-07-15", "hsa_bal": 500.00},
        {"name": "Jane Doe",   "due_amount": 3200.00, "due_date": "2025-07-20", "hsa_bal": 200.00},
        {"name": "Aditya Gupta",   "due_amount": 200.00, "due_date": "2025-07-30", "hsa_bal": 250.00},
        {"name": "Vivek Joshi",   "due_amount": 3000.00, "due_date": "2025-08-20", "hsa_bal": 125.00},
        {"name": "Abhinav",   "due_amount": 5000.00, "due_date": "2025-08-10", "hsa_bal": 900.00},
    ]
    matches = [r for r in sample if name.lower() in r["name"].lower()]
    flow_manager.state["records"] = matches
    if len(matches) == 1:
        next_node = create_validate_node()
    elif len(matches) > 1:
        next_node = create_multiple_node()
    else:
        next_node = create_no_node()
    return matches, next_node

async def validate_customer_handler(
    args: FlowArgs, flow_manager: FlowManager
) -> tuple[CustomerRecord, NodeConfig]:
    record = flow_manager.state["records"][0]
    flow_manager.state["customer"] = record
    return record, create_collection_node()


async def ask_user_to_setup_plan_handler(args: FlowArgs, flow_manager: FlowManager) -> tuple[PaymentPlanDetails, NodeConfig]:
    user_response = args["user_response"]
    result = SetupPaymentPlan(user_response=user_response)
    if str(user_response).lower() == 'true':
        due_amount = flow_manager.state["customer"].get('due_amount')
        six_monthly_installment = round(due_amount/6, 2)
        start_date = date.today()
        six_end_date = start_date + relativedelta(months=6)
        twelve_monthly_installment = round(due_amount/12, 2)
        twelve_end_date = start_date + relativedelta(months=12)
        result = {
            'six_monthly_installment': six_monthly_installment,
            'start_date': start_date.strftime('%Y-%m-%d'),
            'six_end_date': six_end_date.strftime('%Y-%m-%d'),
            'twelve_monthly_installment': twelve_monthly_installment,
            'twelve_end_date': twelve_end_date.strftime('%Y-%m-%d')
        }
        flow_manager.state["plan_type"] = result
        next_node = create_plan_discussion_node(result)
    else:
        result = {
            'six_monthly_installment': None,
            'start_date': None,
            'six_end_date': None,
            'twelve_monthly_installment': None,
            'twelve_end_date': None
        }
        next_node = deny_node()

    return result, next_node

async def setup_payment_plan_handler(args: FlowArgs, flow_manager: FlowManager) -> tuple[PaymentPlanDetails, NodeConfig]:
    plan_type = args["plan_type"]
    due_amount = flow_manager.state["customer"].get('due_amount')
    if plan_type.lower() == 'six_months':
        # plan_type = 'six_months'
        monthly_installment = round(due_amount/6, 2)
        start_date = date.today()
        end_date = start_date + relativedelta(months=6)
        result = {
            'monthly_installment': monthly_installment,
            'start_date': start_date.strftime('%Y-%m-%d'),
            'end_date': end_date.strftime('%Y-%m-%d')
        }
        # flow_manager.state["plan_type"] = result
        next_node = six_month_setup_node()
    elif plan_type.lower() == 'twelve_months':
        # plan_type = 'twelve_months'
        monthly_installment = round(due_amount/12, 2)
        start_date = date.today()
        end_date = start_date + relativedelta(months=12)
        result = {
            'monthly_installment': monthly_installment,
            'start_date': start_date.strftime('%Y-%m-%d'),
            'end_date': end_date.strftime('%Y-%m-%d')
        }
        # flow_manager.state["plan_type"] = result
        next_node = twelve_month_setup_node()
    else:
        result = {
            'monthly_installment': None,
            'start_date': None,
            'end_date': None
        }
        flow_manager.state["plan_type"] = result
        next_node = deny_node()

    return result, next_node
# Node configurations using FlowsFunctionSchema
def create_initial_node() -> NodeConfig:
    """Create the initial node asking for age."""
    return {
        "name": "initial",
        "role_messages": [
            {
                "role": "system",
                "content": (
                    "You are Joanna, an empathetic voice-based debt collection assistant. "
                    "Calls are recorded. Speak naturally with human-like pauses, "
                    "silently call any functions never mention them aloud. "
                    "and there gonna be some instructions present in user section follow those instruction properly without mentioning them aloud. "
                    "Your output will be converted to audio so don't include special characters in your answers. "
                    "Always keep the generation of text short like 2 - 3 senctences only."
                ),
            }
        ],
        "task_messages": [
            {
                "role": "user",
                "content": (
                    "[INSTRUCTION: Start the conversation with introducing yourself first]"
                    "Hello! I'm Joanna from abc. I'll guide you through your account today. May I have your full name, please?"),
            },
        ],
        "functions": [
            FlowsFunctionSchema(
                name="fetch_customer_records",
                description="Lookup customer records by name",
                properties={"name": {"type": "string"}},
                required=["name"],
                handler=fetch_customer_records_handler,
            )
        ],
    }


def create_ask_name_node() -> NodeConfig:
    """Create node for Asking Name."""
    return {
        "name": "ask_name",
        "task_messages": [
            {
                "role": "user",
                "content": (
                    "[INSTRUCTION: Ask the user for their name. You are already in the middle of the conversation, make sure generated text will feel like natural and continuous part of conversation, without using words like 'Certainly'.]"
                ),
            }
        ],
        "functions": [
            FlowsFunctionSchema(
                name="fetch_customer_records",
                description="Lookup customer records by name",
                properties={"name": {"type": "string"}},
                required=["name"],
                handler=fetch_customer_records_handler,
            )
        ],
    }


def create_multiple_node() -> NodeConfig:
    return {
        "name": "multiple_matches",
        "task_messages": [
            {
                "role": "user",
                "content": (
                    "[INSTRUCTION: If multiple records are found for a name, ask the user for their full name. You are already in the middle of the conversation, make sure generated text will feel like natural and continuous part of conversation, without using words like 'Certainly'.]"
                ),
            }
        ],
        "functions": [
            FlowsFunctionSchema(
                name="fetch_customer_records",
                description="Lookup customer records by name",
                properties={"name": {"type": "string"}},
                required=["name"],
                handler=fetch_customer_records_handler,
            )
        ],
    }


def create_no_node() -> NodeConfig:
    return {
        "name": "no_matches",
        "task_messages": [
            {
                "role": "user",
                "content": (
                    "[INSTRUCTION: If no matching record is found, then inform the user that there no records avaialble with this name. And ask for name again. You are already in the middle of the conversation, make sure generated text will feel like natural and continuous part of conversation, without using words like 'Certainly'.]"
                ),
            }
        ],
        "functions": [
            FlowsFunctionSchema(
                name="fetch_customer_records",
                description="Lookup customer records by name",
                properties={"name": {"type": "string"}},
                required=["name"],
                handler=fetch_customer_records_handler,
            )
        ],
    }


def create_validate_node() -> NodeConfig:
    return {
        "name": "validate_record",
        "task_messages": [
            {
                "role": "user",
                "content": (
                    "[INSTRUCTION: Validate the information from the record. You are already in the middle of the conversation, make sure generated text will feel like natural and continuous part of conversation, without using words like 'Certainly'.]"
                ),
            }
        ],
        "functions": [
                    FlowsFunctionSchema(
                        name="validate_customer",
                        description="Confirm exact customer match",
                        properties={"name": {"type": "string"}},
                        required=["name"],
                        handler=validate_customer_handler,
                    )
                ],
    }

def create_collection_node() -> NodeConfig:
    return {
        "name": "collection_flow",
        "task_messages": [
            {
                "role": "user",
                "content": (
                    "[INSTRUCTION: Inform the user about the fetched information. You are already in the middle of the conversation, make sure generated text will feel like natural and continuous part of conversation, without using words like 'Certainly'.] "
                    "hi {{customer.name}}. You have {{customer.due_amount | to_words}} dollars "
                    "due on {{customer.due_date | format_date('%B %d, %Y')}}. "
                    "Would you like to set up a payment plan today?"
                ),
            }
        ],
        "functions": [
                    FlowsFunctionSchema(
                        name="ask_user_to_setup_plan",
                        description="If user like to setup a payment plan then it's a True otherwise it's a False.",
                        properties={"user_response": {"type": "boolean"}},
                        required=["user_response"],
                        handler=ask_user_to_setup_plan_handler,
                    )
                ],
    }

def create_plan_discussion_node(plan: PaymentPlanDetails) -> NodeConfig:
    return {
        "name": "plan_discussion",
        "task_messages": [
            {
                "role": "user",
                "content": (
                    "[INSTRUCTION: Propose the customer with six month payment plan and twelve month payment plan and follow the plan selection flow. User can also deny both of these plans. To get the plan details first call setup_payment_plan_handler function. "
                    "You are already in the middle of the conversation, make sure generated text will feel like natural and continuous part of conversation, without using words like 'Certainly'.] " 
                    "I can offer a six months payment plan at {{plan.six_monthly_installment | to_words}} dollars per month from {{plan.start_date | format_date('%B %d, %Y')}} until {{plan.six_end_date | format_date('%B %d, %Y')}}. "
                    "Does that work for you? "
                    "Or twelve months plan at {{plan.twelve_monthly_installment | to_words}} dollars per month from {{plan.start_date | format_date('%B %d, %Y')}} until {{plan.twelve_end_date | format_date('%B %d, %Y')}}?"
                )
            }
        ],
        "functions": [
                    FlowsFunctionSchema(
                        name="setup_payment_plan",
                        description="Choose type of payment plan.",
                        properties={"plan_type": {"type": "string", "enum": ["six_months", "twelve_months", "deny"]}},
                        required=["plan_type"],
                        handler=setup_payment_plan_handler,
                    )
                ],
    }

def six_month_setup_node() -> NodeConfig:
    return {
        "name": "six_month_accept",
        "task_messages": [{
                "role": "user",
                "content": (
                    "[INSTRUCTION: Inform the customer about setting up of six months plan and ask to use HSA balance towards the payment. You are already in the middle of the conversation, make sure generated text will feel like natural and continuous part of conversation, without using words like 'Certainly'.] "
                    "Great! I have set up your six months plan. Would you like to use your HSA balance of {{customer.hsa_bal | to_words}} dollars towards this payment?" 
                )
            }
        ],
    }

def twelve_month_setup_node() -> NodeConfig:
    return {
        "name": "twelve_month_accept",
        "task_messages": [{
                "role": "user",
                "content": (
                    "[INSTRUCTION: Inform the customer about setting up of twelve months plan and ask to use HSA balance towards the payment. You are already in the middle of the conversation, make sure generated text will feel like natural and continuous part of conversation, without using words like 'Certainly'.] "
                    "Great! I have enrolled you in the twelve months plan. Would you like to use your HSA balance of {{customer.hsa_bal | to_words}} dollars towards this payment?" 
                )
            }
        ],
    }

def deny_node() -> NodeConfig:
    return {
        "name": "all_denied",
        "task_messages": [{
                "role": "user",
                "content": (
                    "[INSTRUCTION: End the conversation by greeting the customer and ask for feel free to reach out.] "
                )
            }
        ],
        "post_actions": [{"type": "end_conversation"}]
    }
    

async def run_example(webrtc_connection: SmallWebRTCConnection):
    logger.info(f"Starting bot")

    # Create a transport using the WebRTC connection
    transport = SmallWebRTCTransport(
        webrtc_connection=webrtc_connection,
        params=TransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            vad_analyzer=SileroVADAnalyzer(),
        ),
    )

    stt = AWSTranscribeSTTService()

   # tts = AWSPollyTTSService(
      #  region="us-east-1",  # only specific regions support generative TTS
     #   voice_id="Joanna",
    #    params=AWSPollyTTSService.InputParams(engine="generative", rate="1.1"),
   # )
    tts = ElevenLabsTTSService(
        api_key=os.getenv("ELEVENLABS_API_KEY", ""),
        voice_id=os.getenv("ELEVENLABS_VOICE_ID", ""),
    )

    llm = AWSBedrockLLMService(
        aws_region="us-east-1",
        model="anthropic.claude-3-5-sonnet-20240620-v1:0",
        params=AWSBedrockLLMService.InputParams(temperature=0.8, latency="standard"),
    )

    context = OpenAILLMContext()
    context_aggregator = llm.create_context_aggregator(context)

    pipeline = Pipeline(
        [
            transport.input(),  # Transport user input
            stt,
            context_aggregator.user(),  # User responses
            llm,  # LLM
            tts,  # TTS
            transport.output(),  # Transport bot output
            context_aggregator.assistant(),  # Assistant spoken responses
        ]
    )

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            enable_metrics=True,
            enable_usage_metrics=True,
        ),
    )

    flow_manager = FlowManager(
        task=task,
        llm=llm,
        context_aggregator=context_aggregator,
        transport=transport,
    )


    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
            logger.info(f"Client connected")
            # Kick off the conversation.
            wait_for_user=False
            await flow_manager.initialize(create_initial_node())

    runner = PipelineRunner(handle_sigint=False)

    await runner.run(task)

@app.get("/health")
async def health_check():
    """Simple health check endpoint."""
    return {"status": "healthy"}

@app.get("/", include_in_schema=False)
async def root_redirect():
    return RedirectResponse(url="/client/")


@app.post("/api/offer")
async def offer(request: dict, background_tasks: BackgroundTasks):
    pc_id = request.get("pc_id")

    if pc_id and pc_id in pcs_map:
        pipecat_connection = pcs_map[pc_id]
        logger.info(f"Reusing existing connection for pc_id: {pc_id}")
        await pipecat_connection.renegotiate(
            sdp=request["sdp"],
            type=request["type"],
            restart_pc=request.get("restart_pc", False),
        )
    else:
        pipecat_connection = SmallWebRTCConnection(ice_servers)
        await pipecat_connection.initialize(sdp=request["sdp"], type=request["type"])

        @pipecat_connection.event_handler("closed")
        async def handle_disconnected(webrtc_connection: SmallWebRTCConnection):
            logger.info(f"Discarding peer connection for pc_id: {webrtc_connection.pc_id}")
            pcs_map.pop(webrtc_connection.pc_id, None)

        # Run example function with SmallWebRTC transport arguments.
        background_tasks.add_task(run_example, pipecat_connection)

    answer = pipecat_connection.get_answer()
    # Updating the peer connection inside the map
    pcs_map[answer["pc_id"]] = pipecat_connection

    return answer


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield  # Run app
    coros = [pc.disconnect() for pc in pcs_map.values()]
    await asyncio.gather(*coros)
    pcs_map.clear()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Pipecat Bot Runner")
    parser.add_argument(
        "--host", default="0.0.0.0", help="Host for HTTP server (default: localhost)"
    )
    parser.add_argument(
        "--port", type=int, default=80, help="Port for HTTP server (default: 7860)"
    )
    args = parser.parse_args()

    uvicorn.run(app, host=args.host, port=args.port)


