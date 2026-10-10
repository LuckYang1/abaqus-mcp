import re

# 在 Abaqus/CAE 内核中执行的代码模板。占位符写作 __NAME__，由 render 一次性替换为 Python 字面量


def render(template: str, **values: object) -> str:
    return re.sub(r"__([A-Z][A-Z_]*)__", lambda match: repr(values[match.group(1).lower()]), template)


SET_WORKDIR = r"""
import os
target = __PATH__
if not os.path.isabs(target):
    raise ValueError('path must be absolute')
if not os.path.isdir(target):
    raise FileNotFoundError(target)
previous = os.getcwd()
os.chdir(target)
result = {'previous': previous, 'current': os.getcwd()}
"""


_JOB_SUMMARY = r"""
def _job_summary(job_name):
    job = mdb.jobs[job_name]
    status = getattr(job, 'status', None)
    item = {'name': job_name, 'caeStatus': str(status).upper() if status is not None else None}
    for attr in ('type', 'model', 'description', 'numCpus', 'numDomains', 'memory', 'explicitPrecision'):
        value = getattr(job, attr, None)
        if value is not None:
            item[attr] = str(value)
    return item
"""


MODEL_INFO = r"""
from abaqus import mdb, session

limit = __LIMIT__
truncated = []
""" + _JOB_SUMMARY + r"""
def _names(repository, label):
    keys = list(repository.keys())
    if len(keys) > limit:
        truncated.append(label)
    return keys[:limit], len(keys)

models = {}
for model_name in mdb.models.keys():
    model = mdb.models[model_name]
    assembly = model.rootAssembly
    repositories = (
        ('parts', model.parts), ('materials', model.materials), ('sections', model.sections),
        ('steps', model.steps), ('loads', model.loads), ('boundary_conditions', model.boundaryConditions),
        ('interactions', model.interactions), ('constraints', model.constraints),
        ('amplitudes', model.amplitudes), ('assembly_instances', assembly.instances),
        ('sets', assembly.sets), ('surfaces', assembly.surfaces),
    )
    info = {'counts': {}}
    for key, repository in repositories:
        names, count = _names(repository, model_name + '.' + key)
        info[key] = names
        info['counts'][key] = count
    part_details = {}
    for part_name in info['parts']:
        part = model.parts[part_name]
        set_names, set_count = _names(part.sets, model_name + '.parts.' + part_name + '.sets')
        surface_names, surface_count = _names(part.surfaces, model_name + '.parts.' + part_name + '.surfaces')
        part_details[part_name] = {
            'cells': len(part.cells), 'faces': len(part.faces), 'edges': len(part.edges),
            'vertices': len(part.vertices), 'sets': set_names, 'surfaces': surface_names,
            'set_count': set_count, 'surface_count': surface_count,
        }
    info['part_details'] = part_details
    models[model_name] = info

result = {
    'models': models,
    'jobs': [_job_summary(name) for name in mdb.jobs.keys()],
    'current_viewport': session.currentViewportName,
    'viewports': list(session.viewports.keys()),
    'list_limit': limit,
    'truncated': truncated,
}
"""


SUBMIT_JOB = r"""
import os, time
from abaqus import mdb
from abaqusConstants import OFF

name = __JOB_NAME__
if name not in mdb.jobs:
    raise KeyError('Unknown job %r; available: %s' % (name, list(mdb.jobs.keys())))
job = mdb.jobs[name]
status = getattr(job, 'status', None)
if status is not None and str(status).upper() in ('SUBMITTED', 'RUNNING', 'CHECK_SUBMITTED', 'CHECK_RUNNING'):
    raise RuntimeError('Job %r is already %s; follow it with monitor_job_status' % (name, str(status).upper()))
submitted_at = time.time()
job.submit(consistencyChecking=OFF)
result = {'jobName': name, 'workingDirectory': os.getcwd(), 'submittedAt': submitted_at,
          'caeStatus': str(job.status).upper()}
"""


JOBS = r"""
import locale, os, re, subprocess, time
from abaqus import mdb

name = __JOB_NAME__
terminate = __TERMINATE__
since = __SINCE__
terminate_timeout = __TERMINATE_TIMEOUT__
root = os.getcwd()
# Abaqus 按系统默认编码写作业文件（中文 Windows 为 cp936，文件头日期含“月”）
file_encoding = locale.getpreferredencoding(False)
""" + _JOB_SUMMARY + r"""
def _status(job):
    status = getattr(job, 'status', None) if job is not None else None
    return str(status).upper() if status is not None else None

def _tail(path, maximum=262144):
    if not os.path.isfile(path):
        return ''
    with open(path, 'rb') as handle:
        handle.seek(0, os.SEEK_END)
        handle.seek(max(0, handle.tell() - maximum))
        return handle.read().decode(file_encoding, 'replace')

def _file_info(path):
    exists = os.path.isfile(path)
    return {'path': path, 'exists': exists, 'modifiedAt': os.path.getmtime(path) if exists else None}

if not name:
    result = {'jobs': [_job_summary(key) for key in mdb.jobs.keys()], 'workingDirectory': root}
else:
    if name != os.path.basename(name) or name in ('.', '..'):
        raise ValueError('job_name must be a single file name')
    job = mdb.jobs[name] if name in mdb.jobs else None
    termination = None
    if terminate:
        if job is None:
            raise ValueError('Termination requires a job registered in the current CAE session')
        if _status(job) in ('COMPLETED', 'ABORTED', 'TERMINATED', 'CHECK_COMPLETED'):
            termination = {'requested': False, 'error': 'Job already has a terminal CAE status; no terminate command sent'}
        else:
            # 使用当前 Abaqus 版本的启动程序执行 terminate，由求解器自行收尾并写出终止记录
            command = [os.environ['ABA_COMMAND'], 'terminate', 'job=' + name]
            completed = subprocess.run(command, cwd=root, capture_output=True, text=True, errors='replace',
                                       timeout=terminate_timeout)
            termination = {'requested': completed.returncode == 0, 'command': ' '.join(command),
                           'returnCode': completed.returncode,
                           'output': (completed.stdout + completed.stderr).strip()[-2000:]}
    paths = {ext: os.path.join(root, name + '.' + ext) for ext in ('log', 'sta', 'dat', 'msg', 'lck')}
    files = {ext: _file_info(path) for ext, path in paths.items()}
    log = _tail(paths['log'])
    sta = _tail(paths['sta'])
    dat = _tail(paths['dat'])
    msg = _tail(paths['msg'])
    cae = _status(job)
    combined = '\n'.join((log, sta, dat, msg))
    diagnostics = [line.strip() for line in combined.splitlines()
                   if re.search(r'^\s*(?:\*{2,}\s*|ABAQUS\s+)(?:ERROR|WARNING)\b', line, re.I)]
    errors = [line for line in diagnostics if re.search(r'\bERROR\b', line, re.I)][-10:]
    warnings = [line for line in diagnostics if re.search(r'\bWARNING\b', line, re.I)][-10:]
    log_upper = log.upper()
    sta_upper = sta.upper()
    msg_upper = msg.upper()
    file_state = None
    if re.search(r'TERMINATE REQUEST RECEIVED|PROCESS TERMINATED BY EXTERNAL REQUEST|USER REQUESTED TERMINATION|ABAQUS JOB \S+ TERMINATED',
                 log_upper + '\n' + msg_upper):
        file_state = 'TERMINATED'
    elif re.search(r'EXITED WITH (?:AN )?ERRORS?|ABAQUS JOB \S+ (?:ABORTED|FAILED)|THE ANALYSIS HAS NOT BEEN COMPLETED|\bA N A L Y S I S\s+T E R M I N A T E D\b',
                   log_upper + '\n' + sta_upper):
        file_state = 'FAILED'
    elif re.search(r'ABAQUS JOB \S+ COMPLETED|THE ANALYSIS HAS COMPLETED SUCCESSFULLY', log_upper + '\n' + sta_upper):
        file_state = 'COMPLETED'
    cae_state = {'COMPLETED': 'COMPLETED', 'CHECK_COMPLETED': 'COMPLETED', 'ABORTED': 'FAILED',
                 'TERMINATED': 'TERMINATED', 'RUNNING': 'RUNNING', 'CHECK_RUNNING': 'RUNNING',
                 'SUBMITTED': 'SUBMITTED'}.get(cae)
    lock_exists = files['lck']['exists']
    terminal_mtime = max([files[ext]['modifiedAt'] for ext in ('log', 'sta') if files[ext]['modifiedAt'] is not None] or [0])
    file_is_fresh = since is not None and terminal_mtime >= since
    effective_file_state = file_state if since is None or file_is_fresh else None
    conflict = bool(effective_file_state and cae_state and (
        (cae_state in ('RUNNING', 'SUBMITTED')) or
        (cae_state in ('COMPLETED', 'FAILED', 'TERMINATED') and cae_state != effective_file_state)))
    if conflict:
        state, source = 'UNKNOWN', 'conflicting CAE and file evidence'
    elif cae_state in ('COMPLETED', 'FAILED', 'TERMINATED') and lock_exists:
        state, source = 'UNKNOWN', 'terminal CAE status while lock file exists'
    elif cae_state in ('COMPLETED', 'FAILED', 'TERMINATED'):
        state, source = cae_state, 'CAE job status'
    elif effective_file_state and not lock_exists and job is None and file_is_fresh:
        state, source = effective_file_state, 'log/status file modified after since'
    elif file_state and not lock_exists and job is None:
        state, source = 'UNKNOWN', 'file terminal marker but current run freshness unverified'
    elif file_state and not lock_exists and job is not None and cae_state is None:
        state, source = 'UNKNOWN', 'terminal file marker without a current CAE run'
    elif effective_file_state and lock_exists:
        state, source = 'UNKNOWN', 'terminal file marker while lock file exists'
    elif cae_state:
        state, source = cae_state, 'CAE job status'
    elif lock_exists:
        state, source = 'UNKNOWN', 'lock file only; process not verified'
    else:
        state, source = 'UNKNOWN', 'insufficient evidence'
    progress = [line.strip() for line in sta.splitlines() if line.strip()]
    numeric = [line for line in progress if re.match(r'^\d+\s+\d+\s+', line)]
    current_step = None
    increment = None
    if numeric:
        match = re.match(r'^(\d+)\s+(\d+)\b', numeric[-1])
        current_step, increment = int(match.group(1)), int(match.group(2))
    result = {
        'jobName': name, 'workingDirectory': root, 'state': state,
        'terminal': state in ('COMPLETED', 'FAILED', 'TERMINATED'),
        'source': source, 'caeStatus': cae, 'fileState': file_state,
        'currentStep': current_step, 'increment': increment,
        'progressTail': progress[-8:], 'errors': errors, 'warnings': warnings,
        'errorCountInTails': len([line for line in diagnostics if re.search(r'\bERROR\b', line, re.I)]),
        'warningCountInTails': len([line for line in diagnostics if re.search(r'\bWARNING\b', line, re.I)]),
        'files': files, 'observedAt': time.time(), 'since': since,
        'runFreshness': 'confirmed by CAE' if source == 'CAE job status' and state in ('COMPLETED', 'FAILED', 'TERMINATED') else ('bounded by since' if file_is_fresh else 'unverified'),
    }
    if termination is not None:
        termination['confirmed'] = state == 'TERMINATED' and not lock_exists
        if termination['requested'] and not termination['confirmed']:
            termination['note'] = ('Abaqus finishes its wrap-up before the job reports TERMINATED and releases the lock file; '
                                   'call monitor_job_status again to confirm.')
        result['termination'] = termination
"""


ODB = r"""
import math, os
from odbAccess import openOdb

path = os.path.abspath(__ODB_PATH__)
step_name = __STEP__
frame_index = __FRAME__
variable = __VARIABLE__
set_name = __SET_NAME__
component = __COMPONENT__
history_region = __HISTORY_REGION__
history_variable = __HISTORY_VARIABLE__
max_points = __MAX_POINTS__

if not os.path.isfile(path):
    raise FileNotFoundError(path)
odb = openOdb(path=path, readOnly=True)
try:
    def _step_meta(name, step):
        domain = str(getattr(step, 'domain', ''))
        start = float(getattr(step, 'totalTime', 0.0)) if domain == 'TIME' else None
        duration = float(getattr(step, 'timePeriod', 0.0)) if domain == 'TIME' else None
        count = len(step.frames)
        indices = list(range(count)) if count <= 5 else sorted(set([0, count // 4, count // 2, (3 * count) // 4, count - 1]))
        frames = [{'index': i, 'frameId': step.frames[i].frameId,
                   'frameValue': step.frames[i].frameValue,
                   'description': str(getattr(step.frames[i], 'description', ''))}
                  for i in indices]
        fields = {}
        if count:
            for frame in (step.frames[0], step.frames[-1]):
                for key in frame.fieldOutputs.keys():
                    field = frame.fieldOutputs[key]
                    fields[key] = {'name': key, 'description': str(field.description), 'type': str(field.type),
                                   'components': list(field.componentLabels),
                                   'validInvariants': [str(item) for item in field.validInvariants],
                                   'positions': sorted(set(str(location.position) for location in field.locations))}
        regions = []
        for key in list(step.historyRegions.keys())[:100]:
            region = step.historyRegions[key]
            regions.append({'name': key, 'variables': list(region.historyOutputs.keys())[:100]})
        return {'name': name, 'description': str(getattr(step, 'description', '')),
                'procedure': str(getattr(step, 'procedure', '')), 'domain': domain,
                'startTime': start, 'endTime': start + duration if start is not None and duration is not None else None,
                'duration': duration, 'frameCount': count, 'frames': frames,
                'fieldOutputs': list(fields.values()), 'historyRegions': regions}

    names = list(odb.steps.keys())
    if step_name and step_name not in odb.steps:
        raise KeyError('Unknown step %r; available: %s' % (step_name, names))
    selected_name = step_name or (names[-1] if names else None)
    info = {'path': path, 'title': str(getattr(odb, 'title', '')),
            'description': str(getattr(odb, 'description', '')),
            'parts': list(odb.parts.keys()), 'instances': list(odb.rootAssembly.instances.keys()),
            'assemblyNodeSets': list(odb.rootAssembly.nodeSets.keys())[:200],
            'assemblyElementSets': list(odb.rootAssembly.elementSets.keys())[:200],
            'instanceSets': {key: {'nodeSets': list(odb.rootAssembly.instances[key].nodeSets.keys())[:100],
                                   'elementSets': list(odb.rootAssembly.instances[key].elementSets.keys())[:100]}
                             for key in list(odb.rootAssembly.instances.keys())[:100]},
            'steps': [_step_meta(name, odb.steps[name]) for name in names]}
    if variable or history_variable:
        if selected_name is None:
            raise ValueError('ODB has no steps')
        step = odb.steps[selected_name]
        info['selectedStep'] = selected_name
    if variable:
        if not step.frames:
            raise ValueError('Selected step has no frames')
        index = frame_index if frame_index >= 0 else len(step.frames) + frame_index
        if not 0 <= index < len(step.frames):
            raise IndexError('frame index out of range; frameCount=%d' % len(step.frames))
        frame = step.frames[index]
        if variable not in frame.fieldOutputs:
            raise KeyError('Field %r unavailable in selected frame; available: %s' % (variable, list(frame.fieldOutputs.keys())))
        field = frame.fieldOutputs[variable]
        if set_name:
            matches = []
            assembly = odb.rootAssembly
            location = str(field.values[0].position) if field.values else ''
            collection_names = ('nodeSets',) if location == 'NODAL' else ('elementSets',)
            for collection_name in collection_names:
                collection = getattr(assembly, collection_name)
                if set_name in collection:
                    matches.append(collection[set_name])
            for instance_name in assembly.instances.keys():
                instance = assembly.instances[instance_name]
                for collection_name in collection_names:
                    collection = getattr(instance, collection_name)
                    for key in collection.keys():
                        if set_name == instance_name + '.' + key:
                            matches.append(collection[key])
            if not matches:
                raise KeyError('Unknown set %r; use assembly set or INSTANCE.SET' % set_name)
            if len(matches) != 1:
                raise ValueError('Set name is ambiguous; use INSTANCE.SET or a unique assembly set')
            field = field.getSubset(region=matches[0])
        if component:
            if component not in field.componentLabels:
                raise KeyError('Unknown component %r; available: %s' % (component, list(field.componentLabels)))
            field = field.getScalarField(componentLabel=component)
        count = 0
        minimum = None
        maximum = None
        for item in field.values:
            data = item.data
            if hasattr(data, '__len__') and not isinstance(data, (str, bytes)):
                if component:
                    raise ValueError('Component extraction returned nonscalar data')
                if str(field.type) not in ('VECTOR', 'SCALAR'):
                    raise ValueError('Tensor field requires a component')
                scalar = math.sqrt(sum(float(part) ** 2 for part in data))
            else:
                scalar = float(data)
            if not math.isfinite(scalar):
                continue
            count += 1
            minimum = scalar if minimum is None else min(minimum, scalar)
            maximum = scalar if maximum is None else max(maximum, scalar)
        info['fieldSummary'] = {'step': selected_name, 'frameIndex': index,
                                'frameValue': frame.frameValue, 'variable': variable,
                                'component': component, 'set': set_name,
                                'quantity': 'component' if component else ('magnitude' if str(field.type) == 'VECTOR' else 'scalar'),
                                'count': count, 'min': minimum, 'max': maximum}
    if history_variable:
        keys = list(step.historyRegions.keys())
        region_key = history_region or (keys[0] if len(keys) == 1 else None)
        if region_key is None:
            raise ValueError('Specify history_region; available: %s' % keys[:100])
        if region_key not in step.historyRegions:
            raise KeyError('Unknown history region %r; available: %s' % (region_key, keys[:100]))
        outputs = step.historyRegions[region_key].historyOutputs
        if history_variable not in outputs:
            raise KeyError('Unknown history variable %r; available: %s' % (history_variable, list(outputs.keys())))
        points = outputs[history_variable].data
        count = len(points)
        indices = list(range(count)) if count <= max_points else sorted(set([round(i * (count - 1) / float(max_points - 1)) for i in range(max_points)]))
        sampled = [[float(points[i][0]), float(points[i][1])] for i in indices]
        values = [float(pair[1]) for pair in points if math.isfinite(float(pair[1]))]
        info['historySummary'] = {'step': selected_name, 'region': region_key, 'variable': history_variable,
                                  'pointCount': count, 'returnedPoints': len(sampled),
                                  'min': min(values) if values else None, 'max': max(values) if values else None,
                                  'points': sampled, 'xAxis': 'step time' if str(getattr(step, 'domain', '')) == 'TIME' else str(getattr(step, 'domain', ''))}
    result = info
finally:
    odb.close()
"""


CAPTURE = r"""
import base64, os, struct, tempfile
from abaqus import session
import abaqusConstants

viewport_name = __VIEWPORT_NAME__
image_format = __IMAGE_FORMAT__
save_path = __SAVE_PATH__
formats = {'PNG': abaqusConstants.PNG, 'TIFF': abaqusConstants.TIFF,
           'SVG': abaqusConstants.SVG, 'EPS': abaqusConstants.EPS, 'PS': abaqusConstants.PS}
if viewport_name:
    if viewport_name not in session.viewports:
        raise KeyError('Unknown viewport %r; available: %s' % (viewport_name, list(session.viewports.keys())))
else:
    viewport_name = session.currentViewportName
if image_format not in formats:
    raise ValueError('Unsupported image format: %s' % image_format)
if image_format in ('SVG', 'EPS', 'PS') and not save_path:
    raise ValueError('SVG/EPS/PS requires save_path')
if save_path:
    save_path = os.path.abspath(save_path)
    if not os.path.isdir(os.path.dirname(save_path)):
        raise FileNotFoundError(os.path.dirname(save_path))
    if not save_path.lower().endswith('.' + image_format.lower()):
        raise ValueError('save_path extension must match image_format')
fd, base = tempfile.mkstemp(prefix='abaqus_mcp_capture_')
os.close(fd)
os.unlink(base)
source = None
destination_temp = None
try:
    session.printToFile(fileName=base, format=formats[image_format],
                        canvasObjects=(session.viewports[viewport_name],))
    for candidate in (base + '.' + image_format.lower(), base + '.' + image_format.upper(), base):
        if os.path.isfile(candidate) and os.path.getsize(candidate):
            source = candidate
            break
    if source is None:
        raise RuntimeError('Abaqus printToFile did not produce the requested image')
    with open(source, 'rb') as handle:
        raw = handle.read()
    width = height = None
    if image_format == 'PNG' and raw[:8] == b'\x89PNG\r\n\x1a\n' and len(raw) >= 24:
        width, height = struct.unpack('>II', raw[16:24])
    if save_path:
        output_fd, destination_temp = tempfile.mkstemp(prefix='.abaqus_mcp_', dir=os.path.dirname(save_path))
        with os.fdopen(output_fd, 'wb') as output:
            output.write(raw)
        os.replace(destination_temp, save_path)
        destination_temp = None
    result = {'viewport': viewport_name, 'format': image_format.lower(),
              'savedPath': save_path, 'sizeBytes': len(raw),
              'width': width, 'height': height,
              'imageBase64': base64.b64encode(raw).decode('ascii') if image_format in ('PNG', 'TIFF') and len(raw) <= 10 * 1024 * 1024 else None}
    if result['imageBase64'] is None and not save_path:
        raise ValueError('Image exceeds 10 MiB; provide save_path')
finally:
    for candidate in (destination_temp, source, base, base + '.' + image_format.lower(), base + '.' + image_format.upper()):
        if candidate and os.path.isfile(candidate):
            os.unlink(candidate)
"""
