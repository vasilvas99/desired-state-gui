import os
import json
import traceback
import uuid
from abc import ABC
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from time import monotonic
from typing import List, Optional
from urllib.parse import urlparse

from nicegui import ui
from paho.mqtt import client as mqttc

from local_file_picker import pick_files_filtered
from merge_manifests import merge

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

AVAILABLE_TABLE_COLUMNS = [
    {"headerName": "ID", "field": "rowId", "checkboxSelection": True},
    {"headerName": "Name", "field": "container.name"},
    {"headerName": "Image", "field": "container.image"},
    {"headerName": "Definition", "field": "container.metadata.file_path"},
]

DEPLOYED_TABLE_COLUMNS = [
    {"headerName": "ID", "field": "rowId"},
    {"headerName": "Name", "field": "container.name"},
    {"headerName": "Image", "field": "container.image"},
]

CUA_INVENTORY_TOPIC = "containersupdate/currentstate"
CUA_UPDATE_INVENTORY_TOPIC = f"{CUA_INVENTORY_TOPIC}/get"

# the global MQTT event loop timer, used by connect/disconnect logic
EVENT_LOOP_TIMER = None

# the MQTT client object
MQTT_CLIENT: mqttc.Client = None


@dataclass
class Message:
    activityId: str
    timestamp: int
    payload: dict

    def inner_dict(self):
        return asdict(self)

    def to_json_str(self):
        return json.dumps(self.inner_dict())

    @classmethod
    def from_defaults(self, payload: dict):
        return Message(str(uuid.uuid4()), int(monotonic()), payload)


@dataclass
class ContainerMetadata(ABC):
    ...


@dataclass
class PartialDesiredStateMeta(ContainerMetadata):
    file_path: Path


@dataclass
class Container:
    name: str
    image: str
    metadata: Optional[ContainerMetadata] = None


@dataclass
class Row:
    rowId: int
    container: Container
    _selected: bool = False

    def toggle_selected(self):
        self._selected = not self._selected

    @property
    def selected(self):
        return self._selected

    @classmethod
    def from_containers_list(self, containers: List[Container]) -> List:
        return [Row(idx, container) for idx, container in enumerate(containers)]


def subscribe_to_updates(client, userdata, flags, rc):
    client.subscribe(CUA_INVENTORY_TOPIC)


def extract_container_info(json_data):
    container_info = []

    # Extract containers from the JSON
    software_nodes = json_data.get("payload", {}).get("softwareNodes", [])
    containers = filter(lambda node: node.get("type") == "CONTAINER", software_nodes)
    for container in containers:
        container_id = container.get("id")
        name = container_id.split(":")[1]  # ids are in the format "container:<name>"
        image = None

        # Extract the image value from the parameters
        for param in container.get("parameters", []):
            if param.get("key") == "image":
                image = param.get("value")
                break

        if container_id and image:
            container_info.append(Container(name, image))
    container_info = sorted(container_info, key=lambda c: c.name)
    return container_info


def on_update_message(client, userdata, msg):
    if msg.topic == CUA_INVENTORY_TOPIC:
        current_state = msg.payload.decode("ascii")
        containers = extract_container_info(json.loads(current_state))
        rows_deployed = Row.from_containers_list(containers)
        DEPLOYED_CONTAINERS_GRID.call_api_method("setRowData", rows_deployed)


def main_update_loop(client: mqttc.Client):
    client.publish(
        CUA_UPDATE_INVENTORY_TOPIC, payload=Message.from_defaults({}).to_json_str()
    )
    client.loop()


def reset_connection_loop():
    global EVENT_LOOP_TIMER
    global MQTT_CLIENT
    if EVENT_LOOP_TIMER is not None:
        EVENT_LOOP_TIMER.deactivate()
        EVENT_LOOP_TIMER = None
    if MQTT_CLIENT is not None:
        MQTT_CLIENT.disconnect()
        MQTT_CLIENT = None
        ui.notify("Disconnected")


def connect(uri, keepalive=60, referesh_rate_s=1):
    global EVENT_LOOP_TIMER
    global MQTT_CLIENT
    uri = urlparse(uri)
    c = mqttc.Client()
    c.on_connect = subscribe_to_updates
    c.on_message = on_update_message
    try:
        if uri.port is None:
            port = 1883
        else:
            port = uri.port
        reset_connection_loop()
        c.connect(uri.hostname, port, keepalive)
        MQTT_CLIENT = c
        EVENT_LOOP_TIMER = ui.timer(
            callback=lambda: main_update_loop(c), interval=referesh_rate_s
        )
        ui.notify("Connected")
    except Exception as e:
        traceback.print_exc()
        print(e)
        ui.notify(f"Could not connect: {e}")


def parse_definition(path: Path):
    "Expects single-container desired state document"
    with open(path) as f:
        definition = json.load(f)
    component = definition["payload"]["domains"][0]["components"][0]
    container = Container(
        component["id"],
        component["config"][0]["value"],
        PartialDesiredStateMeta(path.as_posix()),
    )
    return container


async def load_container_definitions():
    definitions = await pick_files_filtered(extension=".json", start_dir=SCRIPT_DIR)
    parsed_definitions = [parse_definition(d) for d in definitions]
    available_rows = Row.from_containers_list(parsed_definitions)
    print(available_rows)
    AVAILABLE_DEFINITIONS_GRID.call_api_method("setRowData", available_rows)


async def deploy_selected_definitions(desired_state_topic):
    global EVENT_LOOP_TIMER
    global MQTT_CLIENT

    if MQTT_CLIENT is None or EVENT_LOOP_TIMER is None:
        ui.notify("Connect to a MQTT broker first!")
        return

    rows = await AVAILABLE_DEFINITIONS_GRID.get_selected_rows()
    print(rows)
    if not rows:
        ui.notify("No selected containers to deploy!")
        return

    selected_paths = list(
        map(lambda row: Path(row["container"]["metadata"]["file_path"]), rows)
    )
    desired_state_msg = merge(selected_paths)
    MQTT_CLIENT.publish(desired_state_topic, payload=json.dumps(desired_state_msg))
    ui.notify("Successfully published desired state!")


async def deploy_selected():
    if rows:
        for row in rows:
            ui.notify(f"{row['name']}, {row['age']}")
    else:
        ui.notify("No rows selected.")


with ui.splitter().classes("w-full") as splitter:
    with splitter.before:
        ui.label("Available Container Definitions")
        AVAILABLE_DEFINITIONS_GRID = ui.aggrid(
            {
                "columnDefs": AVAILABLE_TABLE_COLUMNS,
                "rowData": [],
                "rowSelection": "multiple",
            }
        )
    with splitter.after:
        ui.label("Deployed Containers")
        DEPLOYED_CONTAINERS_GRID = ui.aggrid(
            {
                "columnDefs": DEPLOYED_TABLE_COLUMNS,
                "rowData": [],
                "rowSelection": "multiple",
            }
        )


with ui.row():
    file_picker = ui.button(
        "Load Definitions", on_click=load_container_definitions, icon="folder_open"
    )
    ui.button(
        "Deploy Selected",
        on_click=lambda: deploy_selected_definitions(
            mqtt_desired_state_topic_field.value
        ),
        icon="send",
    )

with ui.row():
    url_text_field = ui.input("Connection Host")
    mqtt_desired_state_topic_field = ui.input("Desired State Topic")

with ui.row():
    ui.button("Connect", on_click=lambda: connect(url_text_field.value), icon="cloud")
    ui.button("Disconnect", on_click=reset_connection_loop, icon="cloud_off")
ui.run(title="Deploy Desired State", reload=False)
