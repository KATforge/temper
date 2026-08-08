from dataclasses import dataclass


@dataclass
class Options:
    command: str = ""
    json: bool = False
    no_input: bool = False
    workspace: str = ""
    yes: bool = False


options = Options()


def configure(*, json_output: bool, no_input: bool, workspace: str, yes: bool, command: str = ""):
    global options
    options = Options(command=command, json=json_output, no_input=no_input, workspace=workspace, yes=yes)
