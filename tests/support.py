import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from mcp.client import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUN_ROOT = PROJECT_ROOT / ".work" / "abaqus-runs" / "tests"
SCRIPTS_DIR = Path(sys.executable).parent

# 测试用 Abaqus 模型：悬臂小梁，long_run 时改为固定小增量，使作业运行足够久以便测试终止
BUILD_MODEL = r"""
from abaqus import mdb
from abaqusConstants import *

model = mdb.Model(name=model_name)
sketch = model.ConstrainedSketch(name='profile', sheetSize=10.0)
sketch.rectangle(point1=(0.0, 0.0), point2=(1.0, 0.1))
part = model.Part(name='Beam', dimensionality=THREE_D, type=DEFORMABLE_BODY)
part.BaseSolidExtrude(sketch=sketch, depth=0.1)
material = model.Material(name='Steel')
material.Elastic(table=((210000.0, 0.3),))
model.HomogeneousSolidSection(name='Section', material='Steel', thickness=None)
part.Set(name='ALL', cells=part.cells)
part.SectionAssignment(region=part.sets['ALL'], sectionName='Section')
part.Set(name='FIXED', faces=part.faces.getByBoundingBox(xMin=-1e-6, xMax=1e-6, yMin=-1.0, yMax=1.0, zMin=-1.0, zMax=1.0))
part.Set(name='TIPFACE', faces=part.faces.getByBoundingBox(xMin=1.0 - 1e-6, xMax=1.0 + 1e-6, yMin=-1.0, yMax=1.0, zMin=-1.0, zMax=1.0))
part.Surface(name='TIP', side1Faces=part.faces.getByBoundingBox(xMin=1.0 - 1e-6, xMax=1.0 + 1e-6, yMin=-1.0, yMax=1.0, zMin=-1.0, zMax=1.0))
part.seedPart(size=0.05)
part.generateMesh()
assembly = model.rootAssembly
assembly.DatumCsysByDefault(CARTESIAN)
instance = assembly.Instance(name='Beam-1', part=part, dependent=ON)
assembly.Set(name='TIP_ASM', faces=instance.faces.getByBoundingBox(xMin=1.0 - 1e-6, xMax=1.0 + 1e-6, yMin=-1.0, yMax=1.0, zMin=-1.0, zMax=1.0))
model.StaticStep(name='Load', previous='Initial')
if long_run:
    model.steps['Load'].setValues(timeIncrementationMethod=FIXED, initialInc=1e-4, maxNumInc=100000)
    model.fieldOutputRequests['F-Output-1'].setValues(frequency=LAST_INCREMENT)
model.EncastreBC(name='Fix', createStepName='Initial', region=instance.sets['FIXED'])
model.Pressure(name='Tip', createStepName='Load', region=instance.surfaces['TIP'], magnitude=1.0)
mdb.Job(name=job_name, model=model_name, numCpus=1)
result = {'model': model_name, 'job': job_name, 'elements': len(part.elements)}
"""

REMOVE_MODEL = r"""
from abaqus import mdb
if job_name in mdb.jobs:
    del mdb.jobs[job_name]
if model_name in mdb.models:
    del mdb.models[model_name]
result = {'models': list(mdb.models.keys()), 'jobs': list(mdb.jobs.keys())}
"""


def server_parameters() -> StdioServerParameters:
    executable = os.environ.get("ABAQUS_MCP_TEST_SERVER")
    command, args = (executable, []) if executable else (sys.executable, ["-m", "abaqus_mcp"])
    return StdioServerParameters(command=command, args=args, env={
        "ABAQUS_MCP_HOST": "127.0.0.1",
        "ABAQUS_MCP_PORT": "48152",
        "ABAQUS_MCP_TIMEOUT": "120",
    })


@asynccontextmanager
async def mcp_session():
    async with stdio_client(server_parameters()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


def payload(result: Any) -> dict[str, Any]:
    return json.loads(result.content[0].text)


def model_code(template: str, model_name: str, job_name: str, long_run: bool = False) -> str:
    return f"model_name = {model_name!r}\njob_name = {job_name!r}\nlong_run = {long_run!r}\n" + template


async def call_ok(session: ClientSession, tool: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    result = await session.call_tool(tool, arguments or {})
    data = payload(result)
    assert not result.is_error, data
    return data
