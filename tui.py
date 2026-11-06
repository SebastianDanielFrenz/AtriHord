import os
import sys
import json
import requests
from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal
from textual.widgets import Header, Footer, Static, Button, ListView, ListItem, Label, Log
from textual.reactive import reactive

SERVER_URL = "http://127.0.0.1:8000"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
USERS_FILE = os.path.join(BASE_DIR, "users.json")

class TaskItem(ListItem):
    def __init__(self, task_data):
        super().__init__(id=f"task_{task_data.get('id')}")
        self.task_data = task_data

    def compose(self) -> ComposeResult:
        t = self.task_data
        category = t.get('category', 'UNKNOWN').upper()
        status = t.get('status', 'Unknown')
        url = t.get('url', '')
        progress = t.get('progress', '')
        
        yield Label(f"[{category}] {status} - {url}", id="title_label")
        yield Label(f"Progress: {progress}", classes="subtext", id="progress_label")

    def update_data(self, new_data):
        self.task_data = new_data
        t = new_data
        category = t.get('category', 'UNKNOWN').upper()
        status = t.get('status', 'Unknown')
        url = t.get('url', '')
        progress = t.get('progress', '')

        try:
            self.query_one("#title_label", Label).update(f"[{category}] {status} - {url}")
            self.query_one("#progress_label", Label).update(f"Progress: {progress}")
        except Exception:
            pass


class PipeItem(ListItem):
    def __init__(self, pipe_name, pdata):
        super().__init__(id=f"pipe_{pipe_name}")
        self.pipe_name = pipe_name
        self.pdata = pdata

    def compose(self) -> ComposeResult:
        yield Label(self._build_text(), id="pipe_label")

    def _build_text(self):
        name = self.pipe_name
        pdata = self.pdata
        return (
            f"• {name.upper():<10} | Q: {pdata.get('queued', 0)} | "
            f"Rate: {pdata.get('rate_mbps', 0.0)} MiB/s | "
            f"Items/h: {pdata.get('items_per_hr', 0.0)} | Total: {pdata.get('total_mb', 0.0)} MB"
        )

    def update_data(self, new_pdata):
        self.pdata = new_pdata
        try:
            self.query_one("#pipe_label", Label).update(self._build_text())
        except Exception:
            pass


class ConsoleTUI(App):
    CSS = """
    Screen {
        background: #0f172a;
    }
    #main_grid {
        layout: grid;
        grid-size: 2 2;
        grid-columns: 1fr 1fr;
        grid-rows: 1fr 1fr;
        padding: 1;
        grid-gutter: 1;
    }
    .panel {
        border: solid #38bdf8;
        background: #1e293b;
        padding: 1;
    }
    .panel-title {
        text-style: bold;
        color: #38bdf8;
        margin-bottom: 1;
    }
    .subtext {
        color: #94a3b8;
    }
    Button {
        margin-right: 1;
        height: 1;
        border: none;
    }
    """

    selected_task_id = reactive(None)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Container(
            Container(
                Static("Active Tasks", classes="panel-title"),
                ListView(id="task_list"),
                classes="panel"
            ),
            Container(
                Static("Pipeline Statuses & Rates", classes="panel-title"),
                ListView(id="pipe_list"),
                classes="panel"
            ),
            Container(
                Static("Task Logs Output", classes="panel-title"),
                Log(id="log_view"),
                classes="panel"
            ),
            Container(
                Static("Queue Controls", classes="panel-title"),
                Horizontal(
                    Button("Prioritize Task", id="btn_prioritize", variant="success"),
                    Button("Terminate Task", id="btn_terminate", variant="error"),
                    Button("Purge Selected Pipe", id="btn_purge", variant="warning")
                ),
                Static("\nUse mouse/arrows to select tasks.", classes="subtext"),
                classes="panel"
            ),
            id="main_grid"
        )
        yield Footer()

    def on_mount(self) -> None:
        self.session = requests.Session()
        self.authenticate()
        self.set_interval(1.0, self.refresh_dashboard)

    def authenticate(self) -> None:
        cookie_found = False
        if os.path.exists(USERS_FILE):
            try:
                with open(USERS_FILE, "r", encoding="utf-8") as f:
                    users = json.load(f)
                if users and users[0].get("cookie"):
                    self.session.cookies.set("auth_session", users[0]["cookie"])
                    cookie_found = True
            except Exception:
                pass

        if not cookie_found:
            username = input("Username: ")
            password = input("Password: ")
            try:
                self.session.post(f"{SERVER_URL}/login", json={"username": username, "password": password})
            except Exception:
                pass

    def refresh_dashboard(self) -> None:
        try:
            # Update Tasks In-Place
            res = self.session.get(f"{SERVER_URL}/api/tasks")
            if res.status_code == 200:
                tasks_res = res.json()
                if isinstance(tasks_res, list):
                    task_list = self.query_one("#task_list", ListView)
                    existing_items = {
                        child.task_data.get("id"): child 
                        for child in task_list.children 
                        if isinstance(child, TaskItem)
                    }
                    
                    new_ids = set()
                    for t in reversed(tasks_res):
                        if not isinstance(t, dict):
                            continue
                        tid = t.get("id")
                        new_ids.add(tid)
                        
                        if tid in existing_items:
                            existing_items[tid].update_data(t)
                        else:
                            task_list.append(TaskItem(t))
                    
                    # Alte, vom Server gelöschte Tasks sicher entfernen
                    for tid, item in existing_items.items():
                        if tid not in new_ids:
                            item.remove()

            # Update Pipes In-Place
            pipe_res = self.session.get(f"{SERVER_URL}/api/pipes")
            if pipe_res.status_code == 200:
                pipes_res = pipe_res.json()
                if isinstance(pipes_res, dict):
                    pipe_list = self.query_one("#pipe_list", ListView)
                    existing_pipes = {
                        child.pipe_name: child 
                        for child in pipe_list.children 
                        if isinstance(child, PipeItem)
                    }
                    
                    for name, pdata in pipes_res.items():
                        if name in existing_pipes:
                            existing_pipes[name].update_data(pdata)
                        else:
                            pipe_list.append(PipeItem(name, pdata))

        except Exception:
            pass

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        item = event.item
        if isinstance(item, TaskItem) and isinstance(item.task_data, dict):
            self.selected_task_id = item.task_data.get("id")
            log_view = self.query_one("#log_view", Log)
            log_view.clear()
            for line in item.task_data.get("logs", []):
                log_view.write_line(line)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "btn_terminate" and self.selected_task_id:
            self.session.post(f"{SERVER_URL}/api/task/{self.selected_task_id}/terminate")
        elif bid == "btn_prioritize" and self.selected_task_id:
            self.session.post(f"{SERVER_URL}/api/task/{self.selected_task_id}/prioritize")

if __name__ == "__main__":
    ConsoleTUI().run()