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
from abaqus import mdb, session
from abaqusConstants import OFF

name = __JOB_NAME__
datacheck = __DATACHECK__
close_open_odb = __CLOSE_OPEN_ODB__
if name not in mdb.jobs:
    raise KeyError('Unknown job %r; available: %s' % (name, list(mdb.jobs.keys())))
job = mdb.jobs[name]
status = getattr(job, 'status', None)
if status is not None and str(status).upper() in ('SUBMITTED', 'RUNNING', 'CHECK_SUBMITTED', 'CHECK_RUNNING'):
    raise RuntimeError('Job %r is already %s; follow it with monitor_job_status' % (name, str(status).upper()))
# 求解器要写出同名 ODB；会话中打开着这个 ODB 时文件被占用
odb_path = os.path.normcase(os.path.abspath(os.path.join(os.getcwd(), name + '.odb')))
open_keys = [key for key in session.odbs.keys()
             if os.path.normcase(os.path.abspath(session.odbs[key].path)) == odb_path]
closed = []
if open_keys and not close_open_odb:
    raise RuntimeError('The ODB of job %r is open in this CAE session (%s); close it with close_odbs or pass '
                       'close_open_odb=true before submitting' % (name, ', '.join(open_keys)))
for key in open_keys:
    session.odbs[key].close()
    closed.append(key)
submitted_at = time.time()
job.submit(consistencyChecking=OFF, datacheckJob=datacheck)
result = {'jobName': name, 'workingDirectory': os.getcwd(), 'submittedAt': submitted_at,
          'caeStatus': str(job.status).upper(), 'datacheck': datacheck, 'closedOdbs': closed}
"""


JOBS = r"""
import locale, os, re, subprocess, time
from abaqus import mdb

name = __JOB_NAME__
terminate = __TERMINATE__
since = __SINCE__
terminate_timeout = __TERMINATE_TIMEOUT__
directory = __DIRECTORY__
processes = __PROCESSES__
diagnostics_wanted = __DIAGNOSTICS__
warning_detail = __WARNING_DETAIL__
group_limit = __GROUP_LIMIT__
if directory:
    if not os.path.isabs(directory):
        raise ValueError('directory must be absolute')
    if not os.path.isdir(directory):
        raise FileNotFoundError(directory)
root = directory or os.getcwd()
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

_MARK = re.compile(r'^\s*\*{2,3}\s*(ERROR|WARNING)\b[:\s]*(.*)$', re.I)
_STEP = re.compile(r'^\s*S T E P\s+(\d+)\b')
_INCREMENT = re.compile(r'\bINCREMENT\s+(\d+)\s+STARTS\b')
_NUMBER = re.compile(r'[-+]?(?:\d+\.?\d*|\.\d+)(?:[EeDd][-+]?\d+)?')

def _key(kind, lines):
    text = ' '.join(' '.join(lines).split())
    return kind + ': ' + _NUMBER.sub('#', text)[:240]

def _record(groups, details, kind, lines, label, line_number, step, increment):
    key = _key(kind, lines)
    group = groups.get(key)
    if group is None:
        group = groups[key] = {'kind': kind, 'key': key, 'count': 0, 'counts': {}, 'firstFile': label,
                               'firstLine': line_number, 'firstStep': step, 'firstIncrement': increment,
                               'text': '\n'.join(lines)[:2000]}
    group['count'] += 1
    group['counts'][label] = group['counts'].get(label, 0) + 1
    if warning_detail and key == warning_detail:
        details.append({'file': label, 'line': line_number, 'step': step, 'increment': increment})

def _scan(path, label, groups, details):
    # 逐行扫描整个 .msg/.dat：一条信息从 ***WARNING/***ERROR 行开始，到空行或下一条 *** 行为止
    if not os.path.isfile(path):
        return
    step = increment = None
    pending = None
    with open(path, 'r', encoding=file_encoding, errors='replace') as handle:
        for line_number, raw in enumerate(handle, 1):
            text = raw.rstrip('\r\n')
            match = _STEP.search(text)
            if match:
                step, increment = int(match.group(1)), None
            match = _INCREMENT.search(text)
            if match:
                increment = int(match.group(1))
            if pending is not None:
                if text.strip() and not text.lstrip().startswith('***') and len(pending[1]) < 20:
                    pending[1].append(text.strip())
                    continue
                _record(groups, details, pending[0], pending[1], label, pending[2], pending[3], pending[4])
                pending = None
            match = _MARK.match(text)
            if match:
                pending = (match.group(1).lower(), [match.group(2).strip()], line_number, step, increment)
    if pending is not None:
        _record(groups, details, pending[0], pending[1], label, pending[2], pending[3], pending[4])

def _diagnostics(paths, log_text):
    groups = {}
    details = []
    _scan(paths['msg'], 'msg', groups, details)
    _scan(paths['dat'], 'dat', groups, details)
    for line_number, line in enumerate(log_text.splitlines(), 1):
        if re.search(r'^\s*Abaqus Error\b', line, re.I):
            _record(groups, details, 'error', [line.strip()], 'log', line_number, None, None)
    ordered = sorted(groups.values(), key=lambda item: -item['count'])
    errors = [item for item in ordered if item['kind'] == 'error']
    warnings = [item for item in ordered if item['kind'] == 'warning']
    data = {'errors': errors[:group_limit], 'warnings': warnings[:group_limit],
            'errorCount': sum(item['count'] for item in errors), 'warningCount': sum(item['count'] for item in warnings),
            'errorGroupCount': len(errors), 'warningGroupCount': len(warnings)}
    if warning_detail:
        data['detail'] = {'key': warning_detail, 'found': warning_detail in groups,
                          'occurrences': details[:500], 'total': len(details)}
    return data

if not name:
    result = {'jobs': [_job_summary(key) for key in mdb.jobs.keys()], 'workingDirectory': root}
else:
    if name != os.path.basename(name) or name in ('.', '..'):
        raise ValueError('job_name must be a single file name')
    # 指定 directory 时按文件与进程判断，不使用 CAE 会话中同名作业的状态
    job = mdb.jobs[name] if name in mdb.jobs and not directory else None
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
    msg = _tail(paths['msg'])
    cae = _status(job)
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
    files_current = since is None or file_is_fresh
    effective_file_state = file_state if files_current else None
    # processes 为 None 表示服务端无法查看求解进程（桥接不在本机）；只保留作业目录与本作业一致的进程
    process_known = processes is not None
    if process_known:
        normalized_root = os.path.normcase(os.path.abspath(root))
        processes = [item for item in processes if not item['directories'] or any(
            os.path.normcase(os.path.abspath(path)) == normalized_root for path in item['directories'])]
    alive = bool(processes)
    freshness = 'unverified'
    if cae_state is not None:
        conflict = bool(effective_file_state and (
            (cae_state in ('RUNNING', 'SUBMITTED')) or
            (cae_state in ('COMPLETED', 'FAILED', 'TERMINATED') and cae_state != effective_file_state)))
        if conflict:
            state, source = 'UNKNOWN', 'conflicting CAE and file evidence'
        elif cae_state in ('COMPLETED', 'FAILED', 'TERMINATED') and lock_exists:
            state, source = 'UNKNOWN', 'terminal CAE status while lock file exists'
        elif cae_state in ('COMPLETED', 'FAILED', 'TERMINATED'):
            state, source, freshness = cae_state, 'CAE job status', 'confirmed by CAE'
        else:
            state, source = cae_state, 'CAE job status'
    elif alive:
        # 作业不属于本次 CAE 运行（命令行提交或指定了 directory），由求解进程与文件判断
        state, source, freshness = 'RUNNING', 'solver process', 'confirmed by solver process'
    elif file_state and files_current:
        state = file_state
        source = 'log/status file modified after since' if file_is_fresh else 'terminal marker in the latest job files'
        freshness = 'bounded by since' if file_is_fresh else 'latest job files'
    elif file_state:
        state, source = 'UNKNOWN', 'terminal file marker older than since'
    elif process_known and files_current and (lock_exists or files['sta']['exists'] or files['log']['exists']):
        state, source = 'DEAD', 'job files without a terminal marker and no solver process'
        freshness = 'bounded by since' if file_is_fresh else 'latest job files'
    elif lock_exists:
        state, source = 'UNKNOWN', 'lock file only; solver processes not visible'
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
        'terminal': state in ('COMPLETED', 'FAILED', 'TERMINATED', 'DEAD'),
        'source': source, 'caeStatus': cae, 'fileState': file_state,
        'currentStep': current_step, 'increment': increment,
        'progressTail': [line for line in progress if re.match(r'^\d+\s+\d+\s+', line) or 'THE ANALYSIS' in line.upper()][-8:],
        'processes': processes,
        'files': files, 'observedAt': time.time(), 'since': since,
        'runFreshness': freshness,
    }
    if diagnostics_wanted:
        result.update(_diagnostics(paths, log))
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
from abaqus import session
normalized = os.path.normcase(path)
# 会话中已打开这个 ODB（可能正显示在视口中）时直接读取它且不关闭；否则只读打开，结束时关闭
already_open = [key for key in session.odbs.keys() if os.path.normcase(os.path.abspath(session.odbs[key].path)) == normalized]
close_after = not already_open
odb = session.odbs[already_open[0]] if already_open else openOdb(path=path, readOnly=True)
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
    info = {'path': path, 'openBeforeCall': already_open, 'title': str(getattr(odb, 'title', '')),
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
    if close_after:
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


PING = r"""
import os, sys, platform
import abaqus
from abaqus import mdb, session

result = {'python': sys.version, 'executable': sys.executable, 'platform': platform.platform(), 'pid': os.getpid(),
          'cpu_count': os.cpu_count(), 'cwd': os.getcwd(), 'abaqus_version': abaqus.version,
          'models': list(mdb.models.keys()), 'viewports': list(session.viewports.keys())}
"""


_MODEL_LOOKUP = r"""
def _model(model_name):
    if model_name not in mdb.models:
        raise KeyError('Unknown model %r; available: %s' % (model_name, list(mdb.models.keys())))
    return mdb.models[model_name]

def _plain(value, depth=0):
    # 基本类型与 Abaqus 常量交给包装代码转换；其他 Abaqus 对象写成简短文本
    if value is None or isinstance(value, (bool, int, float, str)) or type(value).__module__ == 'symbolicConstants':
        return value
    if isinstance(value, (tuple, list)) and depth < 3:
        items = [_plain(item, depth + 1) for item in value[:50]]
        return items + ['... %d more' % (len(value) - 50)] if len(value) > 50 else items
    text = str(value)
    return text if len(text) <= 200 else text[:200] + '...'

def _members(obj):
    data = {}
    for attr in dir(obj):
        if attr.startswith('_'):
            continue
        try:
            value = getattr(obj, attr)
        except Exception:
            continue
        if callable(value):
            continue
        data[attr] = _plain(value)
    return data
"""


STEP_STATES = r"""
import fnmatch
from abaqus import mdb

model_name = __MODEL__
kinds = __KINDS__
names = __NAMES__
step_patterns = __STEPS__
max_entries = __MAX_ENTRIES__
""" + _MODEL_LOOKUP + r"""
REPOSITORIES = {
    'loads': ('loads', 'loadStates'),
    'boundary_conditions': ('boundaryConditions', 'boundaryConditionStates'),
    'interactions': ('interactions', 'interactionStates'),
    'field_outputs': ('fieldOutputRequests', 'fieldOutputRequestStates'),
    'history_outputs': ('historyOutputRequests', 'historyOutputRequestStates'),
    'predefined_fields': ('predefinedFields', 'predefinedFieldStates'),
}
STEP_ATTRIBUTES = ('procedure', 'description', 'timePeriod', 'nlgeom', 'response', 'timeIncrementationMethod',
                   'initialInc', 'minInc', 'maxInc', 'maxNumInc', 'utol', 'cetol', 'creep', 'amplitude',
                   'extrapolation', 'matrixSolver', 'matrixStorage', 'solutionTechnique', 'stabilizationMethod',
                   'stabilizationMagnitude', 'adaptiveDampingRatio', 'continueDampingFactors')
unknown = [kind for kind in kinds if kind not in REPOSITORIES]
if unknown:
    raise ValueError('Unknown kinds %s; choose from %s' % (unknown, sorted(REPOSITORIES)))
model = _model(model_name)

# 分析步顺序：从 Initial 起沿 previous 链排列
following = {}
for step_name in model.steps.keys():
    following.setdefault(getattr(model.steps[step_name], 'previous', None), []).append(step_name)
order = ['Initial'] if 'Initial' in model.steps.keys() else []
while order and order[-1] in following and len(order) <= len(model.steps):
    order.append(following[order[-1]][0])
selected_steps = [name for name in order if not step_patterns or any(fnmatch.fnmatchcase(name, pattern) for pattern in step_patterns)]

def _state_entry(step_name, state):
    status = str(getattr(state, 'status', ''))
    values = {}
    flagged = False
    for attr in dir(state):
        if attr.startswith('_') or not attr.endswith('State') or attr == 'status':
            continue
        flagged = True
        if str(getattr(state, attr)) == 'SET':
            base = attr[:-len('State')]
            if hasattr(state, base):
                values[base] = _plain(getattr(state, base))
    if not flagged and status in ('CREATED', 'MODIFIED'):
        values = {key: value for key, value in _members(state).items() if key != 'status'}
    return {'step': step_name, 'status': status, 'values': values}

objects = []
per_step = {name: {'created': {}, 'modified': {}, 'deactivated': {}} for name in selected_steps}
truncated = False
for kind in kinds:
    repository_name, states_name = REPOSITORIES[kind]
    repository = getattr(model, repository_name)
    for object_name in repository.keys():
        if names and not any(fnmatch.fnmatchcase(object_name, pattern) for pattern in names):
            continue
        if len(objects) >= max_entries:
            truncated = True
            break
        obj = repository[object_name]
        history = []
        create_step = None
        for step_name in order:
            states = getattr(model.steps[step_name], states_name)
            if object_name not in states.keys():
                continue
            state = states[object_name]
            try:
                state.status
            except KeyError:
                # Abaqus 在对象尚未生效的分析步（例如 Initial）中也列出名称，但读取状态时抛出 KeyError
                continue
            entry = _state_entry(step_name, state)
            if entry['status'] == 'CREATED' and create_step is None:
                create_step = step_name
            bucket = {'CREATED': 'created', 'MODIFIED': 'modified', 'DEACTIVATED': 'deactivated'}.get(entry['status'])
            if step_name in per_step and bucket:
                per_step[step_name][bucket].setdefault(kind, []).append(object_name)
            if step_name in selected_steps and (entry['status'] != 'PROPAGATED' or entry['values']):
                history.append(entry)
        objects.append({'kind': kind, 'name': object_name, 'type': type(obj).__name__, 'createStep': create_step,
                        'attributes': _members(obj), 'steps': history})

steps = []
for step_name in selected_steps:
    step = model.steps[step_name]
    attributes = {attr: _plain(getattr(step, attr)) for attr in STEP_ATTRIBUTES if hasattr(step, attr)}
    # 读取 Initial 分析步的 control 会抛出 AbaqusException
    control = getattr(step, 'control', None) if type(step).__name__ != 'InitialStep' else None
    if control is not None and hasattr(control, 'discontinuous'):
        attributes['control.discontinuous'] = _plain(control.discontinuous)
    steps.append({'name': step_name, 'type': type(step).__name__, 'previous': getattr(step, 'previous', None),
                  'attributes': attributes, **per_step[step_name]})

result = {'model': model_name, 'stepOrder': order, 'steps': steps, 'objects': objects,
          'objectCount': len(objects), 'truncated': truncated}
"""


WRITE_INPUT = r"""
import inpParser, locale, os, re
from abaqus import mdb
from abaqusConstants import OFF

model_name = __MODEL__
directory = __DIRECTORY__
job_name = __JOB_NAME__
keywords = __KEYWORDS__
name_filter = __NAME_FILTER__
max_data_lines = __MAX_DATA_LINES__
max_blocks = __MAX_BLOCKS__
keep_file = __KEEP_FILE__
""" + _MODEL_LOOKUP + r"""
model = _model(model_name)
directory = directory or os.getcwd()
if not os.path.isabs(directory):
    raise ValueError('directory must be absolute')
if not os.path.isdir(directory):
    raise FileNotFoundError(directory)
job_name = job_name or 'mcp_inp_' + re.sub(r'[^A-Za-z0-9_]', '_', model_name)[:30]
if not re.match(r'^[A-Za-z][A-Za-z0-9_-]*$', job_name):
    raise ValueError('job_name must start with a letter and contain only letters, digits, _ and -')
if job_name in mdb.jobs:
    raise ValueError('A job named %r already exists in this CAE session; choose another job_name' % job_name)
path = os.path.join(directory, job_name + '.inp')
previous = os.getcwd()
os.chdir(directory)
try:
    mdb.Job(name=job_name, model=model_name)
    try:
        mdb.jobs[job_name].writeInput(consistencyChecking=OFF)
    except Exception as exc:
        restart_job = getattr(model, 'restartJob', None)
        if restart_job:
            raise RuntimeError('%s -- this model restarts from job %r; run that job first so that its .odb, .res, '
                               '.mdl, .stt and .prt files exist in %s' % (abaqus_error_text(exc), restart_job, directory)) from exc
        raise
finally:
    os.chdir(previous)
    if job_name in mdb.jobs:
        del mdb.jobs[job_name]

def _normalize(keyword):
    # inpParser 给出的关键字名为小写且去掉空格，例如 *Model Change 为 modelchange
    return ''.join(str(keyword).lstrip('*').lower().split())

def _row(row):
    return [item if isinstance(item, (int, float, str)) else str(item)
            for item in (row if isinstance(row, (tuple, list)) else (row,))]

wanted = set(_normalize(keyword) for keyword in keywords)
pattern = re.compile(name_filter, re.I) if name_filter else None
counts = {}
matches = []
match_count = 0

def _walk(sequence, parent):
    global match_count
    for keyword in sequence:
        normalized = _normalize(keyword.name)
        counts[normalized] = counts.get(normalized, 0) + 1
        selected = (wanted or pattern is not None) and (not wanted or normalized in wanted) and (
            pattern is None or pattern.search(str(keyword.parameter.get('name', ''))))
        if selected:
            match_count += 1
            if len(matches) < max_blocks:
                rows = [_row(row) for row in keyword.data[:max_data_lines]]
                matches.append({'keyword': normalized, 'parameters': dict(keyword.parameter), 'parent': parent,
                                'comments': [str(line) for line in keyword.comments][:20],
                                'dataLines': len(keyword.data), 'data': rows,
                                'truncated': len(keyword.data) > max_data_lines})
        _walk(keyword.suboptions, normalized)

parser = inpParser.InputFile(path)
_walk(parser.parse(), None)
size = os.path.getsize(path)
if not keep_file:
    os.remove(path)
result = {'path': path if keep_file else None, 'sizeBytes': size,
          'writtenEncoding': locale.getpreferredencoding(False),
          'includes': list(parser.includes), 'missingIncludes': list(parser.missingIncludes),
          'keywordCounts': counts, 'matchCount': match_count, 'matches': matches,
          'note': 'Keywords come from the Abaqus inpParser module: names are lower case without spaces and '
                  'parameter values are upper case; read the file at path for the exact text.'}
"""


FIND_KEYWORD_BLOCKS = r"""
import re
from abaqus import mdb

model_name = __MODEL__
pattern = __PATTERN__
context = __CONTEXT__
max_blocks = __MAX_BLOCKS__
max_chars = __MAX_CHARS__
""" + _MODEL_LOOKUP + r"""
block = _model(model_name).keywordBlock
block.synchVersions(storeNodesAndElements=False)
blocks = list(block.sieBlocks)
regex = re.compile(pattern, re.I | re.M)

def _first_line(index):
    text = blocks[index].strip()
    return text.splitlines()[0][:160] if text else ''

found = [index for index, text in enumerate(blocks) if regex.search(text)]
matches = []
for index in found[:max_blocks]:
    text = blocks[index]
    item = {'index': index, 'length': len(text), 'text': text[:max_chars], 'truncated': len(text) > max_chars}
    if context:
        item['before'] = [{'index': i, 'firstLine': _first_line(i)} for i in range(max(0, index - context), index)]
        item['after'] = [{'index': i, 'firstLine': _first_line(i)} for i in range(index + 1, min(len(blocks), index + 1 + context))]
    matches.append(item)
result = {'model': model_name, 'blockCount': len(blocks), 'matchCount': len(found), 'matches': matches}
"""


EDIT_KEYWORD_BLOCK = r"""
import re
from abaqus import mdb

model_name = __MODEL__
action = __ACTION__
anchor = __ANCHOR__
text = __TEXT__
expect_count = __EXPECT_COUNT__
""" + _MODEL_LOOKUP + r"""
block = _model(model_name).keywordBlock
block.synchVersions(storeNodesAndElements=False)
blocks = list(block.sieBlocks)
if isinstance(anchor, int):
    if not 0 <= anchor < len(blocks):
        raise IndexError('anchor %d is outside 0..%d' % (anchor, len(blocks) - 1))
    positions = [anchor]
else:
    regex = re.compile(anchor, re.I | re.M)
    positions = [index for index, value in enumerate(blocks) if regex.search(value)]
if len(positions) != expect_count:
    preview = [{'index': index, 'firstLine': blocks[index].strip().splitlines()[0][:160] if blocks[index].strip() else ''}
               for index in positions[:10]]
    raise ValueError('anchor matched %d blocks but expect_count is %d; nothing was changed. Matches: %s'
                     % (len(positions), expect_count, preview))
warnings = []
if action in ('insert_after', 'insert_before', 'replace'):
    if not text.strip():
        raise ValueError('text must not be empty for %s' % action)
    duplicates = [index for index, value in enumerate(blocks) if value.strip() == text.strip()]
    if duplicates and action != 'replace':
        raise ValueError('An identical block already exists at %s; nothing was changed' % duplicates)
    if re.search(r'^\*\s*(node|element)\b', text, re.I | re.M):
        warnings.append('The text contains *Node or *Element data. Keyword blocks synchronized with '
                        'storeNodesAndElements=False drop those data lines from the written input file; put node '
                        'and element data in a separate file and reference it with *Include.')
if action == 'insert_before' and 0 in positions:
    raise ValueError('insert_before cannot target block 0')
for position in sorted(positions, reverse=True):
    if action == 'insert_after':
        block.insert(position=position, text=text)
    elif action == 'insert_before':
        block.insert(position=position - 1, text=text)
    elif action == 'replace':
        block.replace(position=position, text=text)
    elif action == 'delete':
        block.replace(position=position, text='')
    else:
        raise ValueError('action must be insert_after, insert_before, replace or delete')
block.synchVersions(storeNodesAndElements=False)
result = {'model': model_name, 'action': action, 'positions': positions, 'blocksBefore': len(blocks),
          'blocksAfter': len(block.sieBlocks), 'warnings': warnings,
          'next': 'Check the written keywords with write_input before submitting.'}
"""


API_HELP = r"""
import ast, glob, inspect, io, locale, os, re, tokenize
from abaqus import mdb, session

target = __TARGET__
rpy_dirs = __RPY_DIRS__
max_examples = __MAX_EXAMPLES__
gui_cwd = globals().get('__abaqus_mcp_gui_cwd__')

def _first(repository):
    keys = list(repository.keys())
    return repository[keys[0]] if keys else None

def _first_in_models(attribute):
    for model_name in mdb.models.keys():
        value = _first(getattr(mdb.models[model_name], attribute))
        if value is not None:
            return value
    return None

def _first_step():
    for model_name in mdb.models.keys():
        steps = mdb.models[model_name].steps
        for step_name in steps.keys():
            if step_name != 'Initial':
                return steps[step_name]
    return None

ROOTS = {
    'Mdb': lambda: mdb, 'Session': lambda: session,
    'Model': lambda: _first(mdb.models),
    'Part': lambda: _first_in_models('parts'),
    'Material': lambda: _first_in_models('materials'),
    'Section': lambda: _first_in_models('sections'),
    'Assembly': lambda: _first(mdb.models).rootAssembly if mdb.models.keys() else None,
    'Step': _first_step,
    'Job': lambda: _first(mdb.jobs),
    'Viewport': lambda: _first(session.viewports),
}
words = [word for word in re.split(r'[.\[\]()\'"]', target) if word]
method = words[-1]
head = target.split('.', 1)[0]
if head in ('mdb', 'session') or '[' in target:
    obj = eval(target, {'mdb': mdb, 'session': session})
elif head in ROOTS:
    obj = ROOTS[head]()
    if obj is None:
        raise LookupError('No %s object exists in this CAE session; pass a full expression such as '
                          "mdb.models['Model-1'].PorePressure" % head)
    for attr in target.split('.')[1:]:
        obj = getattr(obj, attr)
else:
    raise ValueError('target must start with mdb, session or one of %s' % sorted(ROOTS))

doc = inspect.getdoc(obj) or getattr(obj, '__doc__', None)
try:
    signature = str(inspect.signature(obj))
except (TypeError, ValueError):
    signature = None

def _read_python(path):
    with open(path, 'rb') as handle:
        raw = handle.read()
    encoding = tokenize.detect_encoding(io.BytesIO(raw).readline)[0]
    if encoding == 'mbcs':
        encoding = locale.getpreferredencoding(False)
    return raw.decode(encoding, 'replace')

directories = [directory for directory in (rpy_dirs or [gui_cwd, os.getcwd()]) if directory]
files = []
for directory in dict.fromkeys(directories):
    files.extend(glob.glob(os.path.join(directory, 'abaqus.rpy*')))
files = sorted(set(files), key=os.path.getmtime, reverse=True)[:10]
examples = []
seen = set()
keyword_usage = {}
unreadable = []
for path in files:
    source = _read_python(path)
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        unreadable.append({'file': path, 'error': str(exc)})
        continue
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, 'id', None)
        if name != method:
            continue
        for keyword in node.keywords:
            if keyword.arg:
                keyword_usage[keyword.arg] = keyword_usage.get(keyword.arg, 0) + 1
        code = ast.get_source_segment(source, node)
        if code in seen or len(examples) >= max_examples:
            continue
        seen.add(code)
        examples.append({'file': path, 'line': node.lineno, 'code': code[:1500]})

result = {'target': target, 'resolvedType': type(obj).__name__, 'signature': signature, 'doc': doc,
          'docNote': 'Abaqus 2024 __doc__ gives outdated keywords for some methods; prefer keywords seen in abaqus.rpy.',
          'rpy': {'directories': list(dict.fromkeys(directories)), 'files': files, 'examples': examples,
                  'keywordUsage': dict(sorted(keyword_usage.items(), key=lambda item: -item[1])),
                  'unreadable': unreadable}}
"""


_ODB_SESSION = r"""
import fnmatch, os
from abaqus import session

def _normal(path):
    return os.path.normcase(os.path.abspath(path))

def _displayed_in(path):
    names = []
    for name in session.viewports.keys():
        shown = session.viewports[name].displayedObject
        shown_path = getattr(shown, 'path', None)
        if shown_path and _normal(shown_path) == _normal(path):
            names.append(name)
    return names

def _describe(key):
    odb = session.odbs[key]
    return {'key': key, 'path': odb.path, 'readOnly': bool(odb.isReadOnly), 'displayedIn': _displayed_in(odb.path)}
"""


LIST_ODBS = _ODB_SESSION + r"""
result = {'odbs': [_describe(key) for key in session.odbs.keys()]}
"""


CLOSE_ODBS = _ODB_SESSION + r"""
pattern = __PATTERN__
include_displayed = __INCLUDE_DISPLAYED__
closed = []
skipped = []
for key in list(session.odbs.keys()):
    item = _describe(key)
    if not fnmatch.fnmatch(_normal(item['path']), os.path.normcase(pattern)):
        continue
    if item['displayedIn'] and not include_displayed:
        skipped.append(item)
        continue
    session.odbs[key].close()
    closed.append(item)
result = {'closed': closed, 'skippedDisplayed': skipped, 'remaining': [_describe(key) for key in session.odbs.keys()]}
"""
