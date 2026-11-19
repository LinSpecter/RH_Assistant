import sqlite3
import os
import sys
import tempfile
from pathlib import Path
from contextlib import closing
import math
import calendar
import queue
import threading
import tkinter as tk
from tkinter import ttk, filedialog, font as tkfont
from datetime import datetime, date, timedelta, timezone


def enable_dpi_awareness():
    """高 DPI 下让 Tk 自己按真实像素绘制，避免整窗位图拉伸发虚。失败时保持系统默认。"""
    if sys.platform != 'win32':
        return
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def set_app_user_model_id(app_id):
    """让 Windows 任务栏把本程序当成独立应用，用 exe 自己的图标而不是 python 的。"""
    if sys.platform != 'win32':
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except Exception:
        pass


def primary_work_area():
    """主显示器的可用区域（已扣掉任务栏），返回 (left, top, right, bottom)。

    打包后的启动图由 bootloader 固定显示在主屏，主窗口必须对齐到同一块屏，
    否则双屏时会出现"启动图在显示器1、主窗口在显示器2"的割裂感。
    """
    if sys.platform != 'win32':
        return None
    try:
        import ctypes
        from ctypes import wintypes
        rect = wintypes.RECT()
        # SPI_GETWORKAREA = 0x0030，只返回主显示器的工作区
        if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
            return rect.left, rect.top, rect.right, rect.bottom
    except Exception:
        pass
    return None


def place_centered_on_primary(window, width, height):
    """把窗口按"外框"居中到主屏可用区。

    Tk 的 geometry('+x+y') 定位的是窗体外框左上角，而 width/height 是客户区尺寸，
    外框还多出标题栏和边框。若按客户区算居中，窗口底部会压进任务栏里。
    """
    window.geometry(f'{width}x{height}')
    area = primary_work_area()
    if not area:
        return
    left, top, right, bottom = area
    window.update_idletasks()
    extra_w, extra_h = 0, 0
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        hwnd = user32.GetAncestor(window.winfo_id(), 2)
        rect = wintypes.RECT()
        if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            extra_w = (rect.right - rect.left) - window.winfo_width()
            extra_h = (rect.bottom - rect.top) - window.winfo_height()
    except Exception:
        pass
    pos_x = left + max(0, (right - left - width - extra_w) // 2)
    pos_y = top + max(0, (bottom - top - height - extra_h) // 2)
    window.geometry(f'{width}x{height}+{pos_x}+{pos_y}')


def local_timestamp(value):
    """统一时间显示：SQLite 的 CURRENT_TIMESTAMP 是 UTC，导出前换算为本地时间。

    'YYYY-MM-DD HH:MM:SS' 视为 UTC；带 'T' 的值由 datetime.now().isoformat() 写入，
    本身就是本地时间，只做格式归一。无法解析时原样返回，不丢数据。
    """
    text = str(value or '').strip()
    if not text:
        return ''
    try:
        if 'T' in text:
            parsed = datetime.fromisoformat(text)
        else:
            parsed = datetime.strptime(text, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
    except ValueError:
        return text
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return f'{parsed:%Y-%m-%d %H:%M:%S}'


def default_database_path():
    base = os.environ.get('LOCALAPPDATA')
    if not base:
        base = os.path.join(os.path.expanduser('~'), 'AppData', 'Local')
    return Path(base) / 'RHWorkTracker' / 'work_tracker.db'


def legacy_database_paths(target):
    """只查旧启动目录和程序目录，不扫描其他用户数据或 EXE 解包目录。"""
    program_dir = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent
    found = []
    for candidate in (Path.cwd() / 'work_tracker.db', program_dir / 'work_tracker.db'):
        candidate = candidate.resolve()
        if candidate != Path(target).resolve() and candidate.is_file() and candidate not in found:
            found.append(candidate)
    return found


def migrate_database(source, target):
    """以 SQLite 一致性快照迁移，旧库保持原样，目标存在时绝不覆盖。"""
    source, target = Path(source).resolve(), Path(target).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError(f'目标数据库已存在，未覆盖：{target}')
    fd, temporary = tempfile.mkstemp(prefix='migration_', suffix='.db', dir=target.parent)
    os.close(fd)
    try:
        with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as original:
            tables = {row[0] for row in original.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {'work_logs', 'todos'} <= tables:
                raise ValueError('旧文件不是有效的 RH 数据库，迁移已停止。')
            with closing(sqlite3.connect(temporary)) as snapshot:
                original.backup(snapshot)
                if snapshot.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise ValueError('数据库完整性检查失败，迁移已停止。')
        backup_dir = target.parent / 'backups'
        backup_dir.mkdir(exist_ok=True)
        backup = backup_dir / f"迁移前备份_{datetime.now():%Y%m%d_%H%M%S_%f}.db"
        import shutil
        shutil.copy2(temporary, backup)
        # Windows rename 在目标已存在时失败，避免另一个实例创建的数据库被覆盖。
        if target.exists():
            raise FileExistsError(f'目标数据库已存在，未覆盖：{target}')
        if os.name == 'nt':
            os.rename(temporary, target)
        else:
            os.link(temporary, target)
        return backup
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def backup_database(connection, directory, prefix='手动备份'):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f'{prefix}_{datetime.now():%Y%m%d_%H%M%S_%f}.db'
    path.touch(exist_ok=False)
    try:
        with closing(sqlite3.connect(path)) as output:
            connection.backup(output)
            if output.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('备份完整性检查失败。')
        return path
    except Exception:
        path.unlink(missing_ok=True)
        raise


def archive_year(connection, db_path, year, remove=False, target=None):
    """年度归档只包含工时；待办通过独立手选归档。"""
    year = int(year)
    if not 1 <= year < date.today().year:
        raise ValueError('只能归档当前年份之前的年度。')
    start, end = f'{year:04d}-01-01', f'{year + 1:04d}-01-01'
    conditions = {
        'work_logs': ('date >= ? AND date < ?', (start, end)),
        'todos': ('0', ()),
    }
    if target is None:
        candidates = sorted((Path(db_path).resolve().parent / 'archives').glob(f'archive_{year}_*.db'))
        canonical = Path(db_path).resolve().parent / 'archives' / f'archive_{year}.db'
        if canonical.exists():
            target = canonical
        elif len(candidates) == 1:
            target = candidates[0]
        elif len(candidates) > 1:
            raise ValueError('该年度存在多个旧归档，请在归档列表中选择要更新的归档后重试。')
        else:
            target = canonical
    return _create_archive(connection, db_path, year, conditions, '工时按工作日期；不含待办', remove, target=target)


def archive_todos(connection, db_path, record_ids, target=None):
    ids = sorted({int(record_id) for record_id in record_ids})
    if not ids:
        raise ValueError('请至少选择一项已完成或已取消任务。')
    # 逐个检查避免 SQLite 参数数量上限；快照写锁中重新检查状态。
    conditions = {'work_logs': ('0', ()), 'todos': ('0', ())}
    return _create_archive(connection, db_path, 0, conditions, '手选已结束待办，不依赖日期', todo_ids=ids, target=target)


def _create_archive(connection, db_path, year, conditions, note, remove=False, todo_ids=None, target=None):
    base = Path(db_path).resolve().parent
    folder = base / 'archives'
    folder.mkdir(exist_ok=True)
    category = str(year) if year else 'todos'
    path = Path(target).resolve() if target is not None else folder / f'archive_{category}_{datetime.now():%Y%m%d_%H%M%S_%f}.db'
    if path.parent != folder.resolve() or path.suffix.lower() != '.db':
        raise ValueError('目标必须是 archives 目录中的归档数据库。')
    fd, temporary = tempfile.mkstemp(prefix='updating_', suffix='.db', dir=folder)
    os.close(fd)
    temporary = Path(temporary)
    try:
        connection.execute('BEGIN IMMEDIATE')
        with closing(sqlite3.connect(Path(db_path).resolve().as_uri() + '?mode=ro', uri=True)) as reader:
            backup = backup_database(reader, base / 'backups', '归档前备份')
        if path.exists():
            with closing(open_archive(path)) as previous:
                if previous.execute('SELECT year FROM archive_info').fetchone()[0] != year:
                    raise ValueError('归档类型或年度不匹配，未覆盖。')
                backup_database(previous, base / 'backups', f'覆盖前归档备份_{path.stem}')
                with closing(sqlite3.connect(temporary)) as output:
                    previous.backup(output)
        with closing(sqlite3.connect(temporary)) as output:
            counts = {}
            with output:
                output.execute('CREATE TABLE IF NOT EXISTS archive_update_rows (table_name TEXT, record_id INTEGER, PRIMARY KEY(table_name,record_id))')
                output.execute('DELETE FROM archive_update_rows')
                for table, (where, params) in conditions.items():
                    schema = connection.execute('SELECT sql FROM sqlite_master WHERE type=\'table\' AND name=?', (table,)).fetchone()[0]
                    if not output.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                        output.execute(schema)
                    source_columns = connection.execute(f'PRAGMA table_info({table})').fetchall()
                    if source_columns != output.execute(f'PRAGMA table_info({table})').fetchall():
                        raise ValueError('归档表结构与主库不同，更新已停止，旧归档保持不变。')
                    expected = {row[0]: row for row in output.execute(f'SELECT * FROM {table}')}
                    records = connection.execute(f'SELECT * FROM {table} WHERE {where}', params).fetchall()
                    if table == 'todos' and todo_ids is not None:
                        records = []
                        for record_id in todo_ids:
                            row = connection.execute('SELECT * FROM todos WHERE id=?', (record_id,)).fetchone()
                            if row is None or row[2] not in ('completed', 'cancelled'):
                                raise ValueError('选中任务不存在或不是已结束状态，归档已停止。')
                            records.append(row)
                    if records:
                        marks = ','.join('?' for _ in records[0])
                        created_index = next(index for index, col in enumerate(source_columns) if col[1] == 'created_at')
                        for row in records:
                            old = output.execute(f'SELECT * FROM {table} WHERE id=?', (row[0],)).fetchone()
                            if old is not None and old[created_index] != row[created_index]:
                                raise ValueError('记录 ID 相同但创建时间不同，可能来自其他数据库，已拒绝覆盖。')
                            output.execute(f'INSERT OR REPLACE INTO {table} VALUES ({marks})', row)
                            if output.execute(f'SELECT * FROM {table} WHERE id=?', (row[0],)).fetchone() != row:
                                raise ValueError('归档内容校验失败，主库未移除数据。')
                            output.execute('INSERT INTO archive_update_rows VALUES (?,?)', (table, row[0]))
                            expected[row[0]] = row
                    if output.execute(f'SELECT * FROM {table} ORDER BY id').fetchall() != [expected[key] for key in sorted(expected)]:
                        raise ValueError('合并后历史记录校验失败，旧归档保持不变。')
                    counts[table] = len(records)
                output.execute('CREATE TABLE IF NOT EXISTS archive_info (year INTEGER, created_at TEXT, note TEXT)')
                output.execute('DELETE FROM archive_info')
                output.execute('INSERT INTO archive_info VALUES (?,?,?)', (year, datetime.now().isoformat(), note))
            if output.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('归档完整性检查失败。')
        if not any(counts.values()):
            raise ValueError('没有符合条件的可归档记录。')
        os.replace(temporary, path)
        if remove:
            for table, (where, params) in conditions.items():
                if todo_ids is not None and table == 'todos':
                    # 手选待办的删除条件是 ID 列表，不走 SQL 片段。
                    connection.executemany('DELETE FROM todos WHERE id=?',
                                           [(record_id,) for record_id in todo_ids])
                    continue
                if where == '0':
                    continue
                connection.execute(f'DELETE FROM {table} WHERE {where}', params)
        connection.commit()
        return path, backup, counts
    except Exception:
        connection.rollback()
        raise
    finally:
        temporary.unlink(missing_ok=True)


def remove_archived_rows(connection, db_path, path):
    """单独确认后的清理：任何归档行发生变化，都拒绝整批清理。"""
    with closing(open_archive(path)) as archived:
        connection.execute('BEGIN IMMEDIATE')
        try:
            rows_by_table = {}
            has_update_rows = archived.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='archive_update_rows'").fetchone()
            for table in ('work_logs', 'todos'):
                if has_update_rows:
                    rows = archived.execute(f'SELECT * FROM {table} WHERE id IN (SELECT record_id FROM archive_update_rows WHERE table_name=?)', (table,)).fetchall()
                else:
                    rows = archived.execute(f'SELECT * FROM {table}').fetchall()
                for row in rows:
                    current = connection.execute(f'SELECT * FROM {table} WHERE id=?', (row[0],)).fetchone()
                    if current != row or (table == 'todos' and current[2] not in ('completed', 'cancelled')):
                        raise ValueError('主库记录已改变或已移除，清理已停止；请重新归档。')
                rows_by_table[table] = rows
            with closing(sqlite3.connect(Path(db_path).resolve().as_uri() + '?mode=ro', uri=True)) as reader:
                backup = backup_database(reader, Path(db_path).resolve().parent / 'backups', '移除归档记录前备份')
            for table, rows in rows_by_table.items():
                connection.executemany(f'DELETE FROM {table} WHERE id=?', [(row[0],) for row in rows])
            connection.commit()
            return backup
        except Exception:
            connection.rollback()
            raise


def open_archive(path):
    connection = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    try:
        connection.execute('PRAGMA query_only=ON')
        connection.execute('SELECT year FROM archive_info').fetchone()
        connection.execute('SELECT id, date FROM work_logs LIMIT 1')
        connection.execute('SELECT id, status FROM todos LIMIT 1')
        return connection
    except Exception:
        connection.close()
        raise


def _write_export(path, writer):
    """先完成临时文件，失败时不损坏已有导出。覆盖由保存对话框确认。"""
    path = Path(path)
    fd, temporary = tempfile.mkstemp(suffix=path.suffix, dir=path.parent)
    os.close(fd)
    try:
        writer(Path(temporary))
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def export_work_excel(connection, path, start=None, end=None, source='当前记录'):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE

    def check_text(value):
        if isinstance(value, str):
            if len(value) > 32767:
                raise ValueError('文本超过 Excel 单元格 32767 字限制，未导出，原记录未改变。')
            if ILLEGAL_CHARACTERS_RE.search(value):
                raise ValueError('记录包含 Excel 不支持的控制字符，未导出，原记录未改变。')

    check_text(source)
    if (start is None) != (end is None):
        raise ValueError('开始、结束日期必须同时填写。')
    if start is not None:
        start, end = date.fromisoformat(start).isoformat(), date.fromisoformat(end).isoformat()
        if start > end:
            raise ValueError('开始日期不能晚于结束日期。')
    where, params = (' WHERE date BETWEEN ? AND ?', (start, end)) if start else ('', ())
    rows = connection.execute('SELECT id,date,project,description,hours,created_at FROM work_logs' + where +
                              ' ORDER BY date,created_at,id', params).fetchall()
    book = Workbook()
    detail = book.active
    detail.title = '工时记录'
    detail.append(['序号', '日期', '项目', '工作内容', '工时（小时）', '创建时间', '原记录ID'])
    daily, projects = {}, {}
    try:
        for sequence, (record_id, day, project, description, hours, created) in enumerate(rows, 1):
            for value in (project, description, created):
                check_text(value)
            detail.append([sequence, date.fromisoformat(day), project or '', description or '', hours,
                           local_timestamp(created), record_id])
            detail.cell(detail.max_row, 2).number_format = 'yyyy.mm.dd'
            daily[day] = daily.get(day, 0) + hours
            name = project or '未命名项目'
            projects[name] = projects.get(name, 0) + hours
        summary = book.create_sheet('汇总')
        summary.append(['数据来源', source])
        summary.append(['查询范围', f'{start} 至 {end}' if start else '全部记录'])
        summary.append(['记录数', len(rows)])
        summary.append(['总工时（小时）', sum(row[4] for row in rows)])
        summary.append([])
        summary.append(['日期', '工时（小时）'])
        for day, hours in sorted(daily.items()):
            summary.append([date.fromisoformat(day), hours])
            summary.cell(summary.max_row, 1).number_format = 'yyyy.mm.dd'
        summary.append([])
        summary.append(['项目', '工时（小时）'])
        for name, hours in sorted(projects.items(), key=lambda item: (-item[1], item[0])):
            summary.append([name, hours])
        for sheet in book:
            sheet.freeze_panes = 'A2'
            for row in sheet:
                for cell in row:
                    if isinstance(cell.value, str):
                        if len(cell.value) > 32767:
                            raise ValueError('文本超过 Excel 单元格 32767 字限制，未导出，原记录未改变。')
                        # 用户正文即使以 = 开头，也必须是文字而不是 Excel 公式。
                        cell.data_type = 's'
                    cell.alignment = Alignment(vertical='top', wrap_text=True)
            for cell in sheet[1]:
                cell.font = Font(bold=True, color='FFFFFF')
                cell.fill = PatternFill('solid', fgColor='2563EB')
        for column, width in {'A': 8, 'B': 15, 'C': 28, 'D': 75, 'E': 18, 'F': 23, 'G': 14}.items():
            detail.column_dimensions[column].width = width
        detail.auto_filter.ref = detail.dimensions
        summary.column_dimensions['A'].width = 38
        summary.column_dimensions['B'].width = 65
        _write_export(path, lambda temporary: book.save(temporary))
    finally:
        book.close()
    return len(rows)


def export_todos_markdown(connection, path, source='当前记录'):
    """导出所选来源的全部任务，不使用工时范围或任务显示过滤。"""
    rows = connection.execute('SELECT id,task,status,priority,due_date,created_at,completed_at,cancel_reason '
                              'FROM todos ORDER BY created_at,id').fetchall()
    statuses = {'pending': '待处理', 'completed': '已完成', 'cancelled': '已取消'}
    priorities = {'high': '高', 'medium': '中', 'low': '低'}

    def literal(value):
        # 缩进代码块保留多行、Markdown 符号和 HTML 原文，避免正文变成指令/链接。
        return '\n'.join('    ' + line for line in str(value or '（无）').split('\n'))

    lines = ['# 待办事项', '', '## 数据来源', '', literal(source), '',
             f'导出时间：{datetime.now():%Y.%m.%d %H:%M}  ', f'任务总数：{len(rows)}', '']
    for sequence, (record_id, task, status, priority, due, created, completed, reason) in enumerate(rows, 1):
        lines += [f'## {sequence}. {statuses.get(status, "未知状态")} · 原记录 ID {record_id}', '',
                  '### 任务内容', '', literal(task), '',
                  f'- 状态：{statuses.get(status, status)}', f'- 优先级：{priorities.get(priority, priority)}',
                  '- 截止时间：', '', literal(due), '', '- 创建时间：', '', literal(local_timestamp(created)), '',
                  '- 完成时间：', '', literal(local_timestamp(completed)), '', '### 取消原因', '', literal(reason), '']
    content = '\n'.join(lines)
    _write_export(path, lambda temporary: temporary.write_text(content, encoding='utf-8'))
    return len(rows)


class WorkTracker:
    def __init__(self, db_name="work_tracker.db"):
        """初始化工具，创建数据库连接和表"""
        self.conn = sqlite3.connect(db_name)
        self.cursor = self.conn.cursor()
        # 本地单进程使用：WAL 减少读写互斥，NORMAL 避免每次提交都强制刷盘。
        for pragma in ('PRAGMA journal_mode=WAL', 'PRAGMA synchronous=NORMAL',
                       'PRAGMA temp_store=MEMORY'):
            self.conn.execute(pragma)
        self._create_tables()

    def _create_tables(self):
        """创建数据表"""
        # 工时记录表
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS work_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                project TEXT,
                description TEXT,
                hours REAL NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        # 待办事项表
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS todos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                priority TEXT DEFAULT 'medium',
                due_date TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                completed_at TEXT
            )
        ''')
        # 兼容已有数据库，仅添加取消原因字段，不改动原有记录。
        columns = {row[1] for row in self.conn.execute('PRAGMA table_info(todos)')}
        if 'cancel_reason' not in columns:
            self.conn.execute("ALTER TABLE todos ADD COLUMN cancel_reason TEXT DEFAULT ''")
        for name, definition in (
            ('reminder_enabled', 'INTEGER NOT NULL DEFAULT 1'),
            ('reminder_snooze_until', 'TEXT'),
            ('reminder_last_day', 'TEXT'),
        ):
            if name not in columns:
                self.conn.execute(f'ALTER TABLE todos ADD COLUMN {name} {definition}')
        self.conn.execute('CREATE INDEX IF NOT EXISTS idx_work_date ON work_logs(date, created_at, id)')
        self.conn.execute('CREATE INDEX IF NOT EXISTS idx_todos_status ON todos(status, created_at, id)')
        # 循环提醒表：与 work_logs / todos 完全独立，新增时绝不触碰老数据。
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                rule TEXT NOT NULL,
                next_fire TEXT,
                fire_time TEXT DEFAULT '09:00',
                enabled INTEGER NOT NULL DEFAULT 1,
                weekend_skip INTEGER NOT NULL DEFAULT 0,
                fired_count INTEGER NOT NULL DEFAULT 0,
                last_fired TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        self.conn.execute('CREATE INDEX IF NOT EXISTS idx_reminders_next ON reminders(next_fire, enabled)')
        self.conn.commit()

    def due_reminders(self, now=None):
        """每天一次；到时的稍后提醒优先，不受当天已提醒限制。"""
        now = now or datetime.now()
        result = []
        rows = self.conn.execute(
            "SELECT id, task, due_date, reminder_snooze_until, reminder_last_day "
            "FROM todos WHERE status='pending' AND reminder_enabled=1 AND due_date IS NOT NULL"
        ).fetchall()
        for record_id, task, due, snooze, last_day in rows:
            try:
                deadline = WorkTrackerApp.parse_deadline(due)
                day = deadline.date()
                if snooze:
                    eligible = datetime.fromisoformat(snooze) <= now
                else:
                    eligible = (deadline - now <= timedelta(days=1) if WorkTrackerApp.has_deadline_time(due) else (day - now.date()).days <= 1) and last_day != now.date().isoformat()
            except (ValueError, TypeError):
                continue
            if eligible:
                result.append((record_id, task, deadline.isoformat(timespec='minutes') if WorkTrackerApp.has_deadline_time(due) else day.isoformat()))
        return sorted(result, key=lambda row: (row[2], row[0]))

    def reminder_action(self, record_id, action, minutes=None, now=None):
        now = now or datetime.now()
        if action == 'snooze':
            if minutes is None or not math.isfinite(minutes) or minutes <= 0:
                raise ValueError('稍后提醒的分钟数必须是大于 0 的数字。')
            try:
                until = now + timedelta(minutes=minutes)
            except OverflowError:
                raise ValueError('提醒时间超出可支持范围。') from None
            if until <= now:
                raise ValueError('提醒间隔过短，请增加分钟数。')
        else:
            until = None
        if action not in ('snooze', 'dismiss', 'disable', 'complete'):
            raise ValueError('未知提醒操作。')
        with self.conn:
            self.conn.execute(
                'UPDATE todos SET reminder_snooze_until=?, reminder_last_day=?, '
                'reminder_enabled=CASE WHEN ? THEN 0 ELSE reminder_enabled END, '
                'status=CASE WHEN ? THEN \'completed\' ELSE status END, '
                'completed_at=CASE WHEN ? THEN ? ELSE completed_at END '
                "WHERE id=? AND status='pending'",
                (until.isoformat() if until else None, now.date().isoformat(),
                 action == 'disable', action == 'complete', action == 'complete', now.isoformat(), record_id)
            )
        return until

    # -------------------------------------------------------------- 循环提醒规则引擎
    @staticmethod
    def compute_next_fire(rule, from_date, weekend_skip=False):
        """按 rule 计算 from_date 之后（含当天）最近的一次触发日期。

        rule 是 dict，支持的 kind：
          once     {kind:'once', date:'2026-12-25'}
          yearly   {kind:'yearly', month:3, day:15}
          monthly  {kind:'monthly', every:2, day:15}
          weekly   {kind:'weekly', every:2, weekday:4}   # weekday: 0=周一 .. 6=周日
          countdown {kind:'countdown', target:'2027-06-07'}
        weekend_skip=True 时，如果算出来是周六/周日，顺延到下一个周一。
        返回 date 对象；如果规则已终止（countdown 过了 target / once 已过）返回 None。
        """
        import json as _json
        if isinstance(rule, str):
            try:
                rule = _json.loads(rule)
            except _json.JSONDecodeError:
                return None
        kind = rule.get('kind')
        today = from_date if isinstance(from_date, date) else from_date.date()

        def _skip_weekend(d):
            if weekend_skip and d.weekday() >= 5:
                # 周六 (5) 顺延 +2 到周一；周日 (6) 顺延 +1 到周一
                d = d + timedelta(days=7 - d.weekday())
            return d

        if kind == 'once':
            d = WorkTracker.parse_date_input(rule.get('date'))
            if d is None:
                return None
            return _skip_weekend(d) if d >= today else None

        if kind == 'yearly':
            month = int(rule.get('month', 1))
            day = int(rule.get('day', 1))
            for year in (today.year, today.year + 1):
                try:
                    d = date(year, month, day)
                except ValueError:
                    continue
                if d >= today:
                    return _skip_weekend(d)
            return None

        if kind == 'monthly':
            every = max(1, int(rule.get('every', 1)))
            day = max(1, min(31, int(rule.get('day', 1))))
            # 从本月份开始试，找到第一个 >= today 的
            year, month = today.year, today.month
            for _ in range(24):  # 最多往后推 24 个月，防止死循环
                try:
                    import calendar as _cal
                    last = _cal.monthrange(year, month)[1]
                    dd = min(day, last)
                    d = date(year, month, dd)
                except ValueError:
                    break
                if d >= today:
                    return _skip_weekend(d)
                month += every
                while month > 12:
                    month -= 12
                    year += 1
            return None

        if kind == 'weekly':
            every = max(1, int(rule.get('every', 1)))
            weekdays = rule.get('weekdays')
            if weekdays is None:
                weekdays = [int(rule.get('weekday', 0))]  # 兼容旧格式
            weekdays = sorted({int(w) for w in weekdays if 0 <= int(w) <= 6})
            if not weekdays:
                return None
            # 参考周锚点：今天所在周的周一；every=1 所有周都触发，every=N 则每隔 N-1 周触发
            monday_ref = today - timedelta(days=today.weekday())
            candidate = today
            # 安全上限：往后推 2 年
            for _ in range(366 * 2):
                if candidate.weekday() in weekdays:
                    week_diff = (candidate - monday_ref).days // 7
                    if week_diff >= 0 and week_diff % every == 0:
                        return _skip_weekend(candidate)
                candidate += timedelta(days=1)
            return None

        if kind == 'countdown':
            target = WorkTracker.parse_date_input(rule.get('target'))
            if target is None:
                return None
            if today > target:
                return None  # 已过倒数终点，自动停用
            return _skip_weekend(today)  # 每天都触发（只要还没到 target 之后）

        return None  # 未知 kind

    @staticmethod
    def parse_date_input(text):
        """同时接受 YYYY.MM.DD 和 YYYY-MM-DD，返回 date 或 None。"""
        if not text:
            return None
        s = str(text).strip().replace('.', '-').replace('/', '-')
        try:
            return date.fromisoformat(s)
        except (ValueError, TypeError):
            return None

    @staticmethod
    def describe_rule(rule):
        """规则 JSON → 人类可读描述（Tab 列表里显示）。"""
        import json as _json
        if isinstance(rule, str):
            try:
                rule = _json.loads(rule)
            except _json.JSONDecodeError:
                return '(规则损坏)'
        kind = rule.get('kind', '?')
        if kind == 'once':
            d = WorkTracker.parse_date_input(rule.get('date'))
            return f'单次 {d.isoformat() if d else "?"}'
        if kind == 'yearly':
            return f'每年 {rule.get("month", "?")}月{rule.get("day", "?")}日'
        if kind == 'monthly':
            every = rule.get('every', 1)
            prefix = '每月' if every == 1 else f'每 {every} 个月'
            return f'{prefix} {rule.get("day", "?")} 号'
        if kind == 'weekly':
            every = rule.get('every', 1)
            weekdays = rule.get('weekdays')
            if weekdays is None:
                weekdays = [rule.get('weekday', 0)]
            weekdays = sorted({int(w) for w in weekdays if 0 <= int(w) <= 6})
            names = ['一', '二', '三', '四', '五', '六', '日']
            weekday_str = '、'.join(f'周{names[w]}' for w in weekdays)
            prefix = '每周' if every == 1 else f'每 {every} 周'
            return f'{prefix} {weekday_str}'
        if kind == 'countdown':
            tgt = WorkTracker.parse_date_input(rule.get('target'))
            return f'倒数 到 {tgt.isoformat() if tgt else "?"}'
        return f'(未知规则 {kind})'

    # ---------- reminders CRUD ----------
    def list_reminders(self, only_enabled=False):
        sql = ('SELECT id, title, rule, next_fire, fire_time, enabled, weekend_skip, '
               'fired_count, last_fired FROM reminders')
        params = ()
        if only_enabled:
            sql += ' WHERE enabled=1 AND next_fire IS NOT NULL'
        sql += ' ORDER BY enabled DESC, next_fire IS NULL, next_fire ASC, id ASC'
        return self.conn.execute(sql, params).fetchall()

    def add_reminder(self, title, rule, fire_time='09:00', weekend_skip=False):
        next_fire = WorkTracker.compute_next_fire(rule, date.today(), weekend_skip)
        if next_fire is None:
            raise ValueError('这条规则算不出下一次触发日期，请检查规则是否合理。')
        import json as _json
        with self.conn:
            cur = self.conn.execute(
                'INSERT INTO reminders (title, rule, next_fire, fire_time, weekend_skip) '
                'VALUES (?, ?, ?, ?, ?)',
                (title, _json.dumps(rule, ensure_ascii=False), next_fire.isoformat(),
                 fire_time, 1 if weekend_skip else 0)
            )
            return cur.lastrowid

    def update_reminder(self, record_id, title=None, rule=None, fire_time=None,
                        weekend_skip=None, enabled=None):
        cur = self.conn.execute('SELECT rule, weekend_skip FROM reminders WHERE id=?', (record_id,))
        row = cur.fetchone()
        if row is None:
            raise KeyError('提醒不存在。')
        import json as _json
        current_rule = _json.loads(row[0])
        current_skip = bool(row[1])
        new_rule = rule if rule is not None else current_rule
        new_skip = weekend_skip if weekend_skip is not None else current_skip
        next_fire = WorkTracker.compute_next_fire(new_rule, date.today(), new_skip)
        if rule is not None and next_fire is None:
            raise ValueError('新规则算不出下一次触发日期。')
        updates = []
        params = []
        if title is not None:
            updates.append('title=?')
            params.append(title)
        if rule is not None:
            updates.append('rule=?')
            params.append(_json.dumps(new_rule, ensure_ascii=False))
        if fire_time is not None:
            updates.append('fire_time=?')
            params.append(fire_time)
        if weekend_skip is not None:
            updates.append('weekend_skip=?')
            params.append(1 if weekend_skip else 0)
        if enabled is not None:
            updates.append('enabled=?')
            params.append(1 if enabled else 0)
        updates.append('next_fire=?')
        params.append(next_fire.isoformat() if next_fire else None)
        params.append(record_id)
        with self.conn:
            self.conn.execute(f'UPDATE reminders SET {",".join(updates)} WHERE id=?', params)

    def delete_reminder(self, record_id):
        with self.conn:
            self.conn.execute('DELETE FROM reminders WHERE id=?', (record_id,))

    def toggle_reminder(self, record_id, enabled):
        with self.conn:
            self.conn.execute('UPDATE reminders SET enabled=? WHERE id=?',
                              (1 if enabled else 0, record_id))

    def consume_due_reminders(self, now=None):
        """返回今天到期的循环提醒，并把每条的 next_fire 推到下一次。

        返回 [(record_id, title, fire_time_str), ...]
        """
        now = now or datetime.now()
        today_str = now.date().isoformat()
        rows = self.conn.execute(
            "SELECT id, title, rule, next_fire, fire_time, weekend_skip, fired_count, last_fired "
            "FROM reminders WHERE enabled=1 AND next_fire<=?",
            (today_str,)
        ).fetchall()
        due = []
        with self.conn:
            for rid, title, rule, nxt, fire_time, skip, fired, last_fired in rows:
                # countdown 过了 target / once 已过 → 自动停用
                import json as _json
                parsed = _json.loads(rule) if isinstance(rule, str) else rule
                if parsed.get('kind') == 'countdown':
                    tgt = WorkTracker.parse_date_input(parsed.get('target'))
                    if tgt and now.date() > tgt:
                        self.conn.execute('UPDATE reminders SET enabled=0 WHERE id=?', (rid,))
                        continue
                if parsed.get('kind') == 'once':
                    # once 触发后标记停用
                    self.conn.execute(
                        'UPDATE reminders SET enabled=0, fired_count=fired_count+1, '
                        'last_fired=? WHERE id=?',
                        (now.isoformat(), rid)
                    )
                    due.append((rid, title, fire_time or '09:00'))
                    continue
                next_next = WorkTracker.compute_next_fire(parsed, now.date() + timedelta(days=1), bool(skip))
                self.conn.execute(
                    'UPDATE reminders SET next_fire=?, fired_count=fired_count+1, '
                    'last_fired=? WHERE id=?',
                    (next_next.isoformat() if next_next else None, now.isoformat(), rid)
                )
                due.append((rid, title, fire_time or '09:00'))
        return due

    # ==================== 工时管理 ====================

    def add_work_log(self, project, description, hours, log_date=None):
        """写入一条工时记录。校验下沉到数据层，避免绕过界面的调用写坏数据。

        date 必须保持 ISO 格式：月历和所有范围查询都依赖 'YYYY-MM-DD' 字符串比较，
        一旦写入其他格式，该记录会在月历与日期查询中凭空消失。
        """
        if log_date is None:
            log_date = date.today().isoformat()
        try:
            parsed_date = date.fromisoformat(str(log_date).strip())
        except (ValueError, TypeError):
            raise ValueError(f'日期格式无效，应为 YYYY-MM-DD：{log_date}')
        try:
            hours = float(hours)
        except (TypeError, ValueError):
            raise ValueError(f'工时必须为数字：{hours}')
        if not math.isfinite(hours) or not 0 < hours <= 24:
            raise ValueError('单条工时需在 0 到 24 小时之间（不含 0）。')
        if not str(project or '').strip():
            raise ValueError('项目名称不能为空。')
        if not str(description or '').strip():
            raise ValueError('工作内容不能为空。')
        self.cursor.execute(
            'INSERT INTO work_logs (date, project, description, hours) VALUES (?, ?, ?, ?)',
            (parsed_date.isoformat(), str(project).strip(), str(description), hours)
        )
        self.conn.commit()
        return self.cursor.lastrowid

    # ==================== 待办事项管理 ====================

    def add_todo(self, task, priority="medium", due_date=None):
        """添加待办事项。"""
        if priority not in ('high', 'medium', 'low'):
            priority = 'medium'
        if due_date:
            try:
                due_date = date.fromisoformat(str(due_date).strip()).isoformat()
            except (ValueError, TypeError):
                # 允许 'YYYY-MM-DDTHH:MM' 这类带时间的截止时间，仅拒绝完全无法解析的值。
                try:
                    datetime.fromisoformat(str(due_date).strip())
                except (ValueError, TypeError):
                    raise ValueError(f'截止日期格式无效：{due_date}')
        if not str(task or '').strip():
            raise ValueError('任务描述不能为空。')
        self.cursor.execute(
            'INSERT INTO todos (task, priority, due_date) VALUES (?, ?, ?)',
            (str(task).strip(), priority, due_date)
        )
        self.conn.commit()
        return self.cursor.lastrowid

    def complete_todo(self, todo_id):
        """标记完成；返回是否确实更新了一行，供界面判断记录是否已不存在。"""
        self.cursor.execute(
            "UPDATE todos SET status = 'completed', completed_at = ? WHERE id = ?",
            (datetime.now().isoformat(timespec='seconds'), todo_id)
        )
        self.conn.commit()
        return self.cursor.rowcount > 0

    def delete_todo(self, todo_id):
        """删除待办；返回是否确实删除了一行。"""
        self.cursor.execute('DELETE FROM todos WHERE id = ?', (todo_id,))
        self.conn.commit()
        return self.cursor.rowcount > 0

    def close(self):
        """关闭数据库连接"""
        self.conn.close()


def _splash_text(message):
    """更新打包后 exe 的启动画面文字；源码运行时静默跳过。"""
    try:
        import pyi_splash
    except Exception:
        return
    try:
        pyi_splash.update_text(message)
    except Exception:
        pass


def _splash_close():
    """关闭启动画面；源码运行时静默跳过。"""
    try:
        import pyi_splash
    except Exception:
        return
    try:
        pyi_splash.close()
    except Exception:
        pass


def _has_splash():
    """当前进程是否带 PyInstaller 启动画面（只有打包后的 exe 才有）。"""
    try:
        import pyi_splash  # noqa: F401
    except Exception:
        return False
    return True


class StartupOverlay:
    """启动遮罩：盖住还在拼装的界面，显示解压之后的真实阶段。

    onefile 打包的 exe 每次启动都要先把整个包解压到临时目录，那几秒发生在
    Python 起来之前，由 PyInstaller 的 splash 负责；这里负责之后的部分。
    源码运行时没有 splash，就靠这块遮罩告诉用户进度。
    """

    def __init__(self, master, steps=4):
        self.steps = max(1, steps)
        self.step = 0
        self.frame = tk.Frame(master, bg='#F8FAFC')
        self.frame.place(x=0, y=0, relwidth=1, relheight=1)
        box = tk.Frame(self.frame, bg='#F8FAFC')
        box.place(relx=0.5, rely=0.42, anchor='center')
        tk.Label(box, text='RH · 每日工作记录', bg='#F8FAFC', fg='#1E40AF',
                 font=('Microsoft YaHei UI', 18, 'bold')).pack()
        self.text = tk.StringVar(value='正在启动…')
        tk.Label(box, textvariable=self.text, bg='#F8FAFC', fg='#475569',
                 font=('Microsoft YaHei UI', 10)).pack(pady=(12, 14))
        self.bar = ttk.Progressbar(box, length=320, maximum=self.steps,
                                   mode='determinate')
        self.bar.pack()
        self.frame.lift()
        master.update_idletasks()

    def next(self, message):
        """推进到下一个阶段并立刻刷到屏幕上"""
        self.step = min(self.step + 1, self.steps)
        try:
            self.text.set(message)
            self.bar['value'] = self.step
            self.frame.master.update_idletasks()
        except tk.TclError:
            pass

    def close(self):
        try:
            self.frame.destroy()
        except tk.TclError:
            pass


class WorkTrackerApp(tk.Tk):
    """工时和待办管理界面；沿用原有 SQLite 表结构。"""

    PRIORITIES = {'高': 'high', '中': 'medium', '低': 'low'}
    STATUSES = {'❓待处理': 'pending', '☑️已完成': 'completed', '❌已取消': 'cancelled'}

    def __init__(self, db_path=None):
        super().__init__()
        self.title('RH · 每日工作记录')
        # 使用内置绘制的记事本图标，无需外部图片；保留引用避免被回收。
        self._app_icon = tk.PhotoImage(master=self, width=32, height=32)
        self._app_icon.put('#2563eb', to=(4, 2, 29, 30))
        self._app_icon.put('#ffffff', to=(7, 4, 27, 28))
        self._app_icon.put('#93c5fd', to=(9, 4, 10, 28))
        for y in (8, 13, 18, 23):
            self._app_icon.put('#64748b', to=(2, y, 8, y + 2))
            self._app_icon.put('#60a5fa', to=(12, y, 24, y + 1))
        # iconphoto(True, ...) 只登记"以后新建窗口的默认图标"，不会作用到当前窗口，
        # 所以主窗口必须用 False 单独设一次，再用 True 让后续弹窗继承。
        self.iconphoto(False, self._app_icon)
        self.iconphoto(True, self._app_icon)
        # 按屏幕可用区域夹取窗口尺寸，小屏幕或高分屏下底栏也不会被顶出可视区。
        screen_w = max(self.winfo_screenwidth(), 800)
        screen_h = max(self.winfo_screenheight(), 600)
        width = min(1400, screen_w - 80)
        # 窗口高度按屏幕可用区与内容实际需求共同决定，避免底栏被内容顶出可视区。
        height = min(max(760, screen_h - 110), 980)
        # 主窗口按外框居中到主屏：启动图也在主屏，两者不会分到两块显示器上。
        place_centered_on_primary(self, width, height)
        self.minsize(min(1100, screen_w - 120), min(620, screen_h - 160))
        # 启动进度：打包后解压那几秒由 splash 负责，这里展示解压后的真实阶段。
        self._startup = None if _has_splash() else StartupOverlay(self, steps=4)
        self._startup_step('正在打开数据库…')
        try:
            if db_path is None:
                target = default_database_path()
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.exists():
                    for source in legacy_database_paths(target):
                        if self.show_message(
                            '首次使用：迁移旧数据',
                            f'发现旧数据库：\n{source}\n\n是否迁移到个人数据目录？\n{target}\n\n旧文件不删除，并会额外保存迁移前备份。\n若不是你的数据，请选择取消。',
                            parent=self, confirm=True
                        ):
                            migrate_database(source, target)
                            break
                db_path = target
            self.db_path = os.path.abspath(db_path)
            self.tracker = WorkTracker(self.db_path)
            self._startup_step('正在加载界面…')
        except (sqlite3.Error, OSError, ValueError) as exc:
            _splash_close()
            self.show_message('数据库打开失败', str(exc), parent=self)
            self.destroy()
            raise

        style = ttk.Style(self)
        if 'vista' in style.theme_names():
            style.theme_use('vista')
        # vista 主题默认字体是 Segoe UI（纯英文字体），中文会 fallback 到另一套字体，
        # 导致同一界面里中英文粗细不一致。这里统一指定雅黑，覆盖所有 ttk 控件。
        style.configure('.', font=('Microsoft YaHei UI', 10))
        # 输入框和下拉框在 vista 主题下单独绑定了 TkTextFont，configure('.') 改不动，
        # 必须改命名字体本身才能生效。
        for named_font in ('TkDefaultFont', 'TkTextFont', 'TkMenuFont',
                           'TkHeadingFont', 'TkCaptionFont'):
            try:
                tkfont.nametofont(named_font).configure(
                    family='Microsoft YaHei UI', size=10)
            except tk.TclError:
                pass
        style.configure('Treeview', rowheight=30, font=('Microsoft YaHei UI', 10))
        style.configure('Treeview.Heading', font=('Microsoft YaHei UI', 10, 'bold'))
        style.configure('Title.TLabel', font=('Microsoft YaHei UI', 16, 'bold'))
        style.configure('Summary.TLabel', font=('Microsoft YaHei UI', 10, 'bold'))

        header = ttk.Frame(self, padding=(12, 8))
        header.pack(fill='x')
        ttk.Label(header, text='每日工作记录', style='Title.TLabel').pack(side='left')
        ttk.Label(header, text='记录工时 · 查看汇总 · 管理待办').pack(side='left', padx=18)
        # 右上角概览：今日 / 本周 / 本月工时与未完成待办，一眼看清进度。
        self.overview_text = tk.StringVar(value='正在统计…')
        ttk.Label(header, textvariable=self.overview_text, style='Summary.TLabel',
                  anchor='e').pack(side='right')
        self.archive_conn = None
        self.archive_paths = {}
        self.readonly_buttons = []
        self.data_source = tk.StringVar(value='当前记录')
        self.source_hint = tk.StringVar(value='当前记录 · 可编辑')
        source_bar = ttk.Frame(self, padding=(12, 4))
        source_bar.pack(fill='x')
        ttk.Label(source_bar, text='数据来源').pack(side='left')
        self.source_combo = ttk.Combobox(source_bar, textvariable=self.data_source, state='readonly', width=38)
        self.source_combo.pack(side='left', padx=8)
        self.source_combo.bind('<<ComboboxSelected>>', self.change_data_source)
        ttk.Label(source_bar, textvariable=self.source_hint).pack(side='left')
        ttk.Button(source_bar, text='返回当前记录', command=self.return_to_current).pack(side='left', padx=8)
        ttk.Button(source_bar, text='数据管理', command=self.show_data_management).pack(side='right')
        self.reload_archive_sources()
        self.status = tk.StringVar(value='就绪')
        self._closing = False
        self._tray = None
        self._tray_ready = threading.Event()
        self._tray_events = queue.Queue()
        self._reminder_window = None
        self._reminder_signature = None
        self._reminder_stamp = None
        self._reminders_dirty = True
        self._undo_state = None
        self._undo_job = None

        # 底栏先以 side='bottom' 占位：pack 按声明顺序分配空间，
        # 这样标签页内容再高也不会把状态栏和退出/托盘按钮顶出可视区。
        footer = ttk.Frame(self, padding=(12, 5))
        footer.pack(side='bottom', fill='x')
        ttk.Label(footer, textvariable=self.status).pack(side='left')
        self._undo_button = ttk.Button(footer, text='撤销删除', command=self._undo_delete)
        ttk.Button(footer, text='数据库位置', command=lambda: self.show_message(
            '数据库位置', self.db_path, parent=self
        )).pack(side='right')
        ttk.Button(footer, text='打开数据目录', command=self.open_data_directory).pack(side='right', padx=4)
        ttk.Button(footer, text='隐藏到托盘', command=self.hide_to_tray).pack(side='right', padx=8)
        ttk.Button(footer, text='彻底退出', command=self.exit_app).pack(side='right')

        self.tabs = tabs = ttk.Notebook(self)
        tabs.pack(side='top', fill='both', expand=True, padx=10)
        self.work_tab = ttk.Frame(tabs, padding=8)
        self.todo_tab = ttk.Frame(tabs, padding=8)
        self.reminder_tab = ttk.Frame(tabs, padding=8)
        tabs.add(self.work_tab, text='  工时记录  ')
        tabs.add(self.todo_tab, text='  待办事项  ')
        tabs.add(self.reminder_tab, text='  🔔 提醒事项  ')
        self._build_work_tab()
        self._build_todo_tab()
        self._build_reminder_tab()
        self._startup_step('正在统计工时…')
        self.protocol('WM_DELETE_WINDOW', self.on_close)
        self._bind_shortcuts()
        self.refresh_work()
        self.refresh_todos()
        self.refresh_reminders()
        self.refresh_overview()
        self._startup_step('正在启动托盘…')
        self._start_tray()
        self._background_id = self.after(1000, self._background_tick)
        # 界面已就绪：撤掉遮罩、关掉启动画面，让主窗口干净地出现
        if self._startup is not None:
            self._startup.close()
            self._startup = None
        _splash_close()

    def _startup_step(self, message):
        """报告启动阶段：有 splash 就写在 splash 上，否则更新遮罩进度条。"""
        if self._startup is not None:
            self._startup.next(message)
        else:
            _splash_text(message)

    def refresh_overview(self):
        """刷新标题栏概览：今日 / 本周 / 本月工时，外加未完成待办数。"""
        try:
            today = date.today()
            week_start = today - timedelta(days=today.weekday())
            month_start = today.replace(day=1)
            rows = self.view_connection.execute(
                'SELECT date, SUM(hours) FROM work_logs GROUP BY date'
            ).fetchall()
            today_hours = 0.0
            week_hours = 0.0
            month_hours = 0.0
            for day, hours in rows:
                try:
                    parsed = date.fromisoformat(day)
                except (TypeError, ValueError):
                    continue
                if parsed == today:
                    today_hours += hours or 0
                if parsed >= week_start:
                    week_hours += hours or 0
                if parsed >= month_start:
                    month_hours += hours or 0
            pending = self.view_connection.execute(
                "SELECT COUNT(*) FROM todos WHERE status='pending'"
            ).fetchone()[0]
            overdue = self.view_connection.execute(
                "SELECT COUNT(*) FROM todos WHERE status='pending' AND due_date IS NOT NULL "
                'AND due_date < ?', (today.isoformat(),)
            ).fetchone()[0]
            parts = [f'今日 {today_hours:g}h', f'本周 {week_hours:g}h', f'本月 {month_hours:g}h']
            if pending:
                parts.append(f'待办 {pending} 项')
            if overdue:
                parts.append(f'逾期 {overdue} 项')
            self.overview_text.set('  ·  '.join(parts))
        except sqlite3.Error:
            self.overview_text.set('概览不可用')

    def _bind_shortcuts(self):
        """全局快捷键：F5 刷新当前页，Ctrl+S 保存工时，Ctrl+1/2 与 Ctrl+Tab 切换标签页。"""
        self.bind_all('<F5>', self._shortcut_refresh)
        self.bind_all('<Control-s>', self._shortcut_save)
        self.bind_all('<Control-S>', self._shortcut_save)
        self.bind_all('<Control-1>', lambda event: self._select_tab(0))
        self.bind_all('<Control-2>', lambda event: self._select_tab(1))
        self.bind_all('<Control-Tab>', lambda event: self._select_tab(None))

    def _current_tab_index(self):
        try:
            return self.tabs.index(self.tabs.select())
        except (tk.TclError, KeyError):
            return 0

    def _select_tab(self, index):
        tabs = self.tabs.tabs()
        if not tabs:
            return
        if index is None:
            index = self._current_tab_index() + 1
        self.tabs.select(tabs[index % len(tabs)])

    def _shortcut_refresh(self, event=None):
        # 有模态窗口时不打断正在编辑的对话框。
        if self._grab_path():
            return
        if self._current_tab_index() == 1:
            self.refresh_todos()
        else:
            self.refresh_work()
        self.status.set('已刷新')

    def _shortcut_save(self, event=None):
        if self._grab_path() or self._current_tab_index() != 0:
            return
        self.save_work()
        return 'break'

    def open_data_directory(self):
        try:
            os.startfile(os.path.dirname(self.db_path))
        except OSError as exc:
            self.show_message('打开目录失败', str(exc), parent=self)

    @staticmethod
    def display_date(value):
        """界面使用点分日期，数据库继续使用 ISO 日期。"""
        if isinstance(value, date):
            return value.strftime('%Y.%m.%d')
        text = str(value or '').strip()
        for fmt in ('%Y-%m-%d', '%Y.%m.%d', '%Y%m%d', '%Y/%m/%d'):
            try:
                return datetime.strptime(text, fmt).strftime('%Y.%m.%d')
            except ValueError:
                continue
        return text

    @staticmethod
    def valid_date(text, optional=False):
        text = text.strip()
        if optional and not text:
            return None
        text = WorkTrackerApp.display_date(text).replace('.', '-')
        try:
            value = date.fromisoformat(text)
            if value.isoformat() != text:
                raise ValueError
            return text
        except ValueError:
            raise ValueError('日期请填写为 YYYY.MM.DD，例如 2026.11.11。') from None

    @staticmethod
    def has_deadline_time(value):
        return 'T' in str(value) or ' ' in str(value).strip()

    @staticmethod
    def parse_deadline(value):
        parts = str(value).strip().replace('T', ' ').split()
        if not parts:
            raise ValueError('请设置截止日期。')
        day = WorkTrackerApp.valid_date(parts[0])
        if len(parts) == 1:
            return datetime.combine(date.fromisoformat(day), datetime.min.time())
        if len(parts) != 2 or len(parts[1]) != 5:
            raise ValueError('截止时间格式应为 HH:MM。')
        return datetime.strptime(f'{day} {parts[1]}', '%Y-%m-%d %H:%M')

    @staticmethod
    def display_deadline(value):
        if not value:
            return ''
        try:
            deadline = WorkTrackerApp.parse_deadline(value)
            return deadline.strftime('%Y.%m.%d %H:%M' if WorkTrackerApp.has_deadline_time(value) else '%Y.%m.%d')
        except ValueError:
            return str(value)

    def deadline_input(self, parent, variable, initial=None):
        frame = ttk.Frame(parent)
        self.date_input(frame, variable).pack(fill='x')
        hour = tk.StringVar(frame)
        minute = tk.StringVar(frame)
        if initial and self.has_deadline_time(initial):
            parsed = self.parse_deadline(initial)
            hour.set(f'{parsed.hour:02d}')
            minute.set(f'{parsed.minute:02d}')
        time_row = ttk.Frame(frame)
        time_row.pack(fill='x', pady=(3, 0))
        ttk.Combobox(time_row, textvariable=hour, values=('',) + tuple(f'{i:02d}' for i in range(24)), width=3, state='readonly').pack(side='left')
        ttk.Label(time_row, text='时').pack(side='left')
        ttk.Combobox(time_row, textvariable=minute, values=('',) + tuple(f'{i:02d}' for i in range(60)), width=3, state='readonly').pack(side='left')
        ttk.Label(time_row, text='分（可选）').pack(side='left')
        return frame, hour, minute

    def deadline_value(self, variable, hour, minute):
        day = self.valid_date(variable.get(), optional=True)
        if not day:
            if hour.get() or minute.get():
                raise ValueError('选择时分前请先设置截止日期。')
            return None
        if not hour.get() and not minute.get():
            return day
        if not hour.get() or not minute.get():
            raise ValueError('请同时选择截止小时和分钟，或将两项都留空。')
        return self.parse_deadline(f'{day} {hour.get()}:{minute.get()}').isoformat(timespec='minutes')

    def date_input(self, parent, variable):
        frame = ttk.Frame(parent)
        frame.columnconfigure(0, weight=1)
        entry = ttk.Entry(frame, textvariable=variable, width=12)
        entry.grid(row=0, column=0, sticky='ew')
        entry.bind('<FocusOut>', lambda event: self.normalize_date_variable(variable))
        ttk.Button(frame, text='日历', width=5, command=lambda: self.choose_date(variable, parent.winfo_toplevel())).grid(row=0, column=1, padx=(4, 0))
        return frame

    def choose_date(self, variable, parent):
        previous_grab = self._python_widget(self._grab_path())
        window = tk.Toplevel(parent)
        window.title('选择截止日期')
        window.transient(parent)
        window.resizable(True, True)
        try:
            selected = date.fromisoformat(self.valid_date(variable.get()))
        except ValueError:
            selected = date.today()
        month = selected.replace(day=1)
        body = ttk.Frame(window, padding=12)
        body.pack(fill='both', expand=True)
        heading = ttk.Frame(body)
        heading.pack(fill='x')
        caption = tk.StringVar(window)
        days_frame = ttk.Frame(body)

        def finish(value=None):
            if value is not None:
                variable.set(self.display_date(value))
            window.destroy()

        def render():
            # Windows strftime may reject Chinese literals under a non-Chinese
            # locale, aborting rendering before any weekday/date widgets exist.
            caption.set(f'{month.year:04d}年{month.month:02d}月')
            for child in days_frame.winfo_children():
                child.destroy()
            for col, label in enumerate(('一', '二', '三', '四', '五', '六', '日')):
                ttk.Label(days_frame, text=label, anchor='center').grid(row=0, column=col, sticky='ew', pady=4)
            weeks = calendar.monthcalendar(month.year, month.month)
            weeks += [[0] * 7 for _ in range(6 - len(weeks))]
            for row, week in enumerate(weeks, 1):
                for col, day in enumerate(week):
                    if day:
                        value = month.replace(day=day)
                        label = f'[{day}]' if value == date.today() else str(day)
                        ttk.Button(days_frame, text=label, width=5, command=lambda value=value: finish(value)).grid(row=row, column=col, padx=1, pady=1)
                    else:
                        ttk.Button(days_frame, text='', width=5, state='disabled').grid(row=row, column=col, padx=1, pady=1)
            # Use actual font/theme metrics; reserve all six weeks on every month.
            window.update_idletasks()
            width, height = window.winfo_reqwidth(), window.winfo_reqheight()
            window.minsize(width, height)
            window.geometry(f'{max(width, window.winfo_width())}x{max(height, window.winfo_height())}')

        def move(delta):
            nonlocal month
            index = month.year * 12 + month.month - 1 + delta
            year, offset = divmod(index, 12)
            if 1 <= year <= 9999:
                month = date(year, offset + 1, 1)
                render()

        ttk.Button(heading, text='‹ 上月', command=lambda: move(-1)).pack(side='left')
        ttk.Label(heading, textvariable=caption, anchor='center').pack(side='left', fill='x', expand=True)
        ttk.Button(heading, text='下月 ›', command=lambda: move(1)).pack(side='right')
        days_frame.pack(pady=8)
        quick = ttk.Frame(body)
        quick.pack(fill='x')
        for label, offset in (('今天', 0), ('明天', 1), ('一周后', 7)):
            ttk.Button(quick, text=label, command=lambda offset=offset: finish(date.today() + timedelta(days=offset))).pack(side='left', padx=2)
        ttk.Button(quick, text='清空', command=lambda: finish('')).pack(side='left', padx=2)
        window.protocol('WM_DELETE_WINDOW', finish)
        window.bind('<Escape>', lambda event: finish())
        render()
        self.center_dialog(window, parent)
        window.grab_set()
        try:
            self.wait_window(window)
        finally:
            if previous_grab is not None and previous_grab.winfo_exists():
                previous_grab.grab_set()

    def normalize_date_variable(self, variable):
        """完整且有效的日期立即规范显示；不干扰尚未完成的输入。"""
        text = variable.get()
        try:
            canonical = self.display_date(self.valid_date(text))
        except ValueError:
            return
        if text != canonical:
            variable.set(canonical)

    @staticmethod
    def reminder_status(status, due, enabled, snooze, last_day, now=None):
        now = now or datetime.now()
        if status != 'pending':
            return '已结束，不提醒'
        if not enabled:
            return '提醒已关闭'
        if not due:
            return '未设截止日期'
        try:
            deadline = WorkTrackerApp.parse_deadline(due)
            due_day = deadline.date()
            if snooze:
                until = datetime.fromisoformat(snooze)
                return f'延后至 {until:%Y.%m.%d %H:%M}' if until > now else '延后已到时，待确认'
        except (ValueError, TypeError):
            return '日期异常，请编辑检查'
        if last_day == now.date().isoformat():
            return '今日已确认，明日再提醒'
        if (deadline - now <= timedelta(days=1) if WorkTrackerApp.has_deadline_time(due) else (due_day - now.date()).days <= 1):
            return '已到提醒时间，待确认'
        return '已启用，提前一天提醒'

    @property
    def view_connection(self):
        return self.archive_conn if self.archive_conn is not None else self.tracker.conn

    def can_edit_current(self):
        if self.archive_conn is not None:
            self.show_message('归档只读', '请先点击“返回当前记录”，再新增、编辑或删除。', parent=self)
            return False
        return True

    def mutation_button(self, parent, **kwargs):
        button = ttk.Button(parent, **kwargs)
        self.readonly_buttons.append(button)
        return button

    def reload_archive_sources(self):
        folder = Path(self.db_path).parent / 'archives'
        self.archive_paths = {}
        if folder.exists():
            for path in sorted(folder.glob('archive_*.db'), reverse=True):
                try:
                    with closing(open_archive(path)) as conn:
                        year, created = conn.execute('SELECT year, created_at FROM archive_info').fetchone()
                    category = f'{year} 年归档' if year else '待办独立归档'
                    label = f'{category} · {created} · {path.stem}'
                    self.archive_paths[label] = path
                except (sqlite3.Error, ValueError, TypeError):
                    continue
        self.source_combo.configure(values=['当前记录', *self.archive_paths])

    def return_to_current(self):
        self.data_source.set('当前记录')
        self.change_data_source()

    def change_data_source(self, event=None):
        new_connection = None
        try:
            path = self.archive_paths.get(self.data_source.get())
            if self.data_source.get() != '当前记录' and path is None:
                raise ValueError('归档不存在，请刷新归档列表。')
            if path is not None:
                new_connection = open_archive(path)
                year = new_connection.execute('SELECT year FROM archive_info').fetchone()[0]
        except (sqlite3.Error, ValueError, OSError) as exc:
            if new_connection is not None:
                new_connection.close()
            self.return_to_current()
            self.show_message('打开归档失败', str(exc), parent=self)
            return
        if self.archive_conn is not None:
            self.archive_conn.close()
        self.archive_conn = new_connection
        category = (f'{year} 年归档' if year else '待办独立归档') if new_connection is not None else ''
        self.source_hint.set(f'{category} · 只读（提醒仍来自主库）' if new_connection is not None else '当前记录 · 可编辑')
        for button in self.readonly_buttons:
            button.configure(state='disabled' if new_connection is not None else 'normal')
        self.work_tree.selection_remove(*self.work_tree.selection())
        self.todo_tree.selection_remove(*self.todo_tree.selection())
        self.query_range.set('全部记录' if new_connection is not None else '今天')
        self.show_completed.set(True)
        self.change_work_range()
        self.refresh_todos()

    def show_query_summary(self):
        try:
            start, end = self.work_query_bounds()
            if start is None:
                start, end = self.view_connection.execute('SELECT MIN(date), MAX(date) FROM work_logs').fetchone()
                if start is None:
                    self.show_message('范围汇总', '当前数据来源暂无工时记录。', parent=self)
                    return
            self._show_period_summary(date.fromisoformat(start), date.fromisoformat(end), '当前查询范围汇总')
        except (ValueError, sqlite3.Error) as exc:
            self.show_message('汇总失败', str(exc), parent=self)

    def export_work(self):
        try:
            start, end = self.work_query_bounds()
            path = filedialog.asksaveasfilename(parent=self, title='导出当前查询范围工时',
                defaultextension='.xlsx', filetypes=[('Excel 工作簿', '*.xlsx')],
                initialfile=f'工时_{start or "全部"}_{end or "记录"}.xlsx')
            if not path:
                return
            count = export_work_excel(self.view_connection, path, start, end, self.data_source.get())
            self.show_message('导出完成', f'已导出 {count} 条工时。\n{path}\n原始数据未改变。', parent=self)
        except ImportError:
            self.show_message('缺少依赖', 'Excel 导出需要 openpyxl，请安装 requirements.txt 中的依赖。', parent=self)
        except (ValueError, OSError, sqlite3.Error) as exc:
            self.show_message('导出失败', str(exc), parent=self)

    def export_todos(self):
        try:
            path = filedialog.asksaveasfilename(parent=self, title='导出当前来源全部待办（含所有状态、无截止日期）',
                defaultextension='.md', filetypes=[('Markdown 文件', '*.md')],
                initialfile=f'全部待办_{date.today():%Y%m%d}.md')
            if not path:
                return
            count = export_todos_markdown(self.view_connection, path, self.data_source.get())
            self.show_message('导出完成', f'已导出当前来源全部 {count} 项任务（不受列表过滤影响）。\n{path}\n原始数据未改变。', parent=self)
        except (ValueError, OSError, sqlite3.Error) as exc:
            self.show_message('导出失败', str(exc), parent=self)

    def show_todo_archive(self, parent=None, on_created=None):
        window = tk.Toplevel(self)
        window.title('独立归档待办 · 从主库选择已完成 / 已取消任务')
        window.geometry('850x500')
        window.transient(parent or self)
        panel = ttk.Frame(window, padding=12)
        panel.pack(fill='both', expand=True)
        ttk.Label(panel, text='Ctrl / Shift 可多选；不要求截止日期。待处理任务不会出现在此列表中。').pack(anchor='w')
        self.reload_archive_sources()
        targets = {}
        for label, path in self.archive_paths.items():
            with closing(open_archive(path)) as archived:
                if archived.execute('SELECT year FROM archive_info').fetchone()[0] == 0:
                    targets[label] = path
        target_choice = tk.StringVar(window, value=next(iter(targets), '新建待办归档'))
        ttk.Label(panel, text='目标归档（选择已有归档则更新同一文件；未选任务的历史内容保留）').pack(anchor='w', pady=(8, 0))
        ttk.Combobox(panel, textvariable=target_choice, values=['新建待办归档', *targets], state='readonly', width=90).pack(fill='x', pady=4)
        tree = self._tree(panel, [('id', '原ID', 65), ('task', '任务', 470), ('status', '状态', 100), ('due', '截止日期', 150)])
        tree.configure(selectmode='extended')
        for record_id, task, status, due in self.tracker.conn.execute(
                "SELECT id,task,status,due_date FROM todos WHERE status IN ('completed','cancelled') ORDER BY created_at,id"):
            tree.insert('', 'end', iid=str(record_id), values=(record_id, task, '已完成' if status == 'completed' else '已取消', due or '未设置'))

        def create():
            try:
                selected = tree.selection()
                if not selected:
                    raise ValueError('请先选择任务。')
                target = targets.get(target_choice.get())
                if not self.show_message('确认归档待办', f'归档选中 {len(selected)} 项任务。\n目标：{target or "新建待办归档"}\n更新已有归档会先备份旧文件，再合并覆盖；其他历史记录保留。\n不直接移除主库记录。', parent=window, confirm=True):
                    return
                if self.archive_conn is not None:
                    self.return_to_current()
                path, backup, counts = archive_todos(self.tracker.conn, self.db_path, selected, target=target)
                self.reload_archive_sources()
                if on_created:
                    on_created()
                if self.show_message('待办归档完成 · 是否移除主库记录',
                        f'已归档 {counts["todos"]} 项。\n归档：{path}\n备份：{backup}\n\n是否从主库移除这些任务？取消则保留。', parent=window, confirm=True):
                    remove_archived_rows(self.tracker.conn, self.db_path, path)
                    self.refresh_todos()
                window.destroy()
            except (ValueError, OSError, sqlite3.Error) as exc:
                self.show_message('归档/清理未完成', str(exc), parent=window)

        controls = ttk.Frame(panel)
        controls.pack(fill='x')
        ttk.Button(controls, text='全选', command=lambda: tree.selection_set(tree.get_children())).pack(side='left')
        ttk.Button(controls, text='归档选中任务', command=create).pack(side='right')
        self.center_dialog(window, parent)
        previous = self._grab_path()
        window.grab_set()
        self.wait_window(window)
        owner = self._python_widget(previous)
        if owner is not None and owner.winfo_exists():
            owner.grab_set()

    def show_data_management(self):
        window = tk.Toplevel(self)
        window.title('数据管理 · 主数据库')
        window.geometry('800x560')
        window.transient(self)
        panel = ttk.Frame(window, padding=16)
        panel.pack(fill='both', expand=True)
        conn = self.tracker.conn
        work_count, oldest = conn.execute('SELECT COUNT(*), MIN(date) FROM work_logs').fetchone()
        task_count = conn.execute('SELECT COUNT(*) FROM todos').fetchone()[0]
        size = sum(p.stat().st_size for p in (Path(self.db_path), Path(self.db_path + '-wal')) if p.exists())
        ttk.Label(panel, text=f'主库：{self.db_path}\n大小：{size / 1024 / 1024:.2f} MB · 工时 {work_count} 条 · 任务 {task_count} 项\n最早工时日期：{oldest or "无"}', wraplength=750).pack(fill='x')
        controls = ttk.Frame(panel)
        controls.pack(fill='x', pady=12)

        def backup_now():
            try:
                path = backup_database(conn, Path(self.db_path).parent / 'backups')
                self.show_message('备份完成', str(path), parent=window)
            except (OSError, sqlite3.Error, ValueError) as exc:
                self.show_message('备份失败', str(exc), parent=window)

        ttk.Button(controls, text='备份主库', command=backup_now).pack(side='left')
        ttk.Button(controls, text='打开数据目录', command=self.open_data_directory).pack(side='left', padx=8)
        year = tk.StringVar(window, value=str(date.today().year - 1))
        ttk.Label(controls, text='归档年度').pack(side='left', padx=(12, 4))
        ttk.Entry(controls, textvariable=year, width=6).pack(side='left')
        ttk.Label(panel, text='工时按往年工作日期归档；同年更新同一文件。多个旧版本时，请先在下方选择要更新的归档。\n待办独立选择目标归档。更新前备份旧文件，保留未更新历史记录；移除主库记录仍需单独确认。', wraplength=750).pack(fill='x')
        archive_list = tk.Listbox(panel, height=10, exportselection=False)
        archive_list.pack(fill='both', expand=True, pady=10)
        paths = []

        def refresh_list():
            self.reload_archive_sources()
            archive_list.delete(0, 'end')
            paths.clear()
            for label, path in self.archive_paths.items():
                archive_list.insert('end', f'{path.name} · {path.stat().st_size / 1024:.1f} KB')
                paths.append(label)

        def create_archive():
            try:
                number = int(year.get())
                if not 1 <= number < date.today().year:
                    raise ValueError('请选择当前年份之前的年度。')
                selected = archive_list.curselection()
                target = self.archive_paths[paths[selected[0]]] if selected else None
                if target is not None:
                    with closing(open_archive(target)) as archived:
                        if archived.execute('SELECT year FROM archive_info').fetchone()[0] != number:
                            raise ValueError('选中归档年度与输入年度不同，请选择正确归档或取消列表选择。')
                if not self.show_message('确认创建/更新工时归档', f'归档 {number} 年工时。\n目标：{target or "自动匹配同年归档（没有则新建）"}\n已有归档先备份，再合并覆盖；未更新的历史记录保留。\n本步骤不会移除主库记录。', parent=window, confirm=True):
                    return
                if self.archive_conn is not None:
                    self.return_to_current()
                path, backup, counts = archive_year(conn, self.db_path, number, target=target)
                refresh_list()
                if self.show_message('归档已创建 · 是否移除主库记录', f'归档：{path}\n备份：{backup}\n工时 {counts["work_logs"]} 条，已结束任务 {counts["todos"]} 项。\n\n是否从主库移除这些已归档记录？\n选“取消”则全部保留。待处理任务不会移除。', parent=window, confirm=True):
                    remove_archived_rows(conn, self.db_path, path)
                    self.refresh_work()
                    self.refresh_todos()
                    self.show_message('清理完成', '已从主库移除经过校验的归档记录；归档和完整备份均已保留。', parent=window)
            except (ValueError, OSError, sqlite3.Error) as exc:
                self.show_message('归档/清理未完成', str(exc), parent=window)

        ttk.Button(controls, text='归档年度工时', command=create_archive).pack(side='left', padx=8)
        actions = ttk.Frame(panel)
        actions.pack(fill='x')

        def view_selected():
            selected = archive_list.curselection()
            if selected:
                self.data_source.set(paths[selected[0]])
                self.change_data_source()
                window.destroy()

        def open_folder():
            folder = Path(self.db_path).parent / 'archives'
            try:
                folder.mkdir(exist_ok=True)
                os.startfile(str(folder))
            except OSError as exc:
                self.show_message('打开失败', str(exc), parent=window)

        ttk.Button(actions, text='查看选中归档（只读）', command=view_selected).pack(side='left')
        ttk.Button(actions, text='刷新归档列表', command=refresh_list).pack(side='left', padx=8)
        ttk.Button(actions, text='打开归档目录', command=open_folder).pack(side='left')
        ttk.Button(actions, text='独立归档待办', command=lambda: self.show_todo_archive(window, refresh_list)).pack(side='right')
        refresh_list()
        self.center_dialog(window)
        window.grab_set()

    def _tree(self, parent, columns):
        frame = ttk.Frame(parent)
        frame.pack(fill='both', expand=True, pady=8)
        tree = ttk.Treeview(frame, columns=[c[0] for c in columns], show='headings', selectmode='browse')
        for key, title, width in columns:
            tree.heading(key, text=title)
            # 让工作内容/任务列吸收多余宽度，避免编号和工时列被无效拉宽。
            flexible = key in ('description', 'task', 'projects', 'project')
            tree.column(key, width=width, minwidth=60, anchor='w', stretch=flexible)
        scroll = ttk.Scrollbar(frame, orient='vertical', command=tree.yview)
        horizontal = ttk.Scrollbar(frame, orient='horizontal', command=tree.xview)
        tree.configure(yscrollcommand=scroll.set, xscrollcommand=horizontal.set)
        tree.grid(row=0, column=0, sticky='nsew')
        scroll.grid(row=0, column=1, sticky='ns')
        horizontal.grid(row=1, column=0, sticky='ew')
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        return tree

    def _build_work_tab(self):
        workspace = ttk.Panedwindow(self.work_tab, orient='horizontal')
        workspace.pack(fill='both', expand=True)
        entry_panel = ttk.Frame(workspace, width=262)
        records_panel = ttk.Frame(workspace)
        workspace.add(entry_panel, weight=0)
        workspace.add(records_panel, weight=1)
        form = ttk.LabelFrame(entry_panel, text='新增工时', padding=10)
        form.pack(fill='both', expand=True, padx=(0, 6))
        self.log_date = tk.StringVar(value=self.display_date(date.today()))
        self.project = tk.StringVar()
        self.hours = tk.StringVar()
        for col, (label, variable, width) in enumerate([
            ('日期（YYYY.MM.DD）', self.log_date, 18),
            ('项目名称', self.project, 32), ('工时（小时）', self.hours, 12)
        ]):
            ttk.Label(form, text=label).grid(row=col * 2, column=0, sticky='w', pady=(4, 0))
            if variable is self.hours:
                # 可选择常用工时，也可直接输入 0.25、1.75 等实际值。
                self.hours_combo = ttk.Combobox(
                    form, textvariable=variable, width=width, state='normal',
                    values=('0.5', '1', '1.5', '2', '2.5', '3', '4', '6', '8')
                )
                self.hours_combo.grid(row=col * 2 + 1, column=0, sticky='ew', pady=(4, 8))
            else:
                ttk.Entry(form, textvariable=variable, width=24).grid(row=col * 2 + 1, column=0, sticky='ew', pady=(4, 8))
        form.columnconfigure(0, weight=1)
        form.rowconfigure(7, weight=1)
        ttk.Label(form, text='工作内容（支持多行）').grid(row=6, column=0, sticky='w', pady=(5, 0))
        editor = ttk.Frame(form)
        editor.grid(row=7, column=0, sticky='nsew', pady=6)
        self.description = tk.Text(editor, width=24, height=5, wrap='word', font=('Microsoft YaHei UI', 10), undo=True)
        editor_scroll = ttk.Scrollbar(editor, orient='vertical', command=self.description.yview)
        self.description.configure(yscrollcommand=editor_scroll.set)
        editor_scroll.pack(side='right', fill='y')
        self.description.pack(side='left', fill='both', expand=True)
        save_row = ttk.Frame(form)
        save_row.grid(row=8, column=0, sticky='ew', pady=(4, 0))
        self.mutation_button(save_row, text='保存工时', command=self.save_work).pack(side='left', fill='x', expand=True)
        ttk.Button(save_row, text='复制上次', command=self.copy_previous_work).pack(side='left', padx=(6, 0))
        self._build_work_calendar(entry_panel)
        bar = ttk.Frame(records_panel)
        bar.pack(fill='x', pady=(0, 4))
        self.query_range = tk.StringVar(value='今天')
        self.query_date = tk.StringVar(value=self.display_date(date.today()))
        self.query_end_date = tk.StringVar(value=self.display_date(date.today()))
        ttk.Label(bar, text='查询范围').pack(side='left')
        range_combo = ttk.Combobox(
            bar, textvariable=self.query_range, state='readonly', width=12,
            values=('今天', '近七天', '本月', '全部记录', '自定义范围')
        )
        range_combo.pack(side='left', padx=8)
        range_combo.bind('<<ComboboxSelected>>', self.change_work_range)
        self.query_start_entry = ttk.Entry(bar, textvariable=self.query_date, width=12, state='disabled')
        self.query_start_entry.pack(side='left')
        ttk.Label(bar, text=' 至 ').pack(side='left')
        self.query_end_entry = ttk.Entry(bar, textvariable=self.query_end_date, width=12, state='disabled')
        self.query_end_entry.pack(side='left', padx=(0, 8))
        ttk.Button(bar, text='查询', command=self.refresh_work).pack(side='left')
        ttk.Button(bar, text='导出工时 Excel', command=self.export_work).pack(side='left', padx=8)

        # 筛选行：按项目 + 关键词缩小结果，实时生效。
        filters = ttk.Frame(records_panel)
        filters.pack(fill='x', pady=(0, 2))
        ttk.Label(filters, text='项目').pack(side='left')
        self.work_project_filter = tk.StringVar(value='全部项目')
        self.work_project_combo = ttk.Combobox(
            filters, textvariable=self.work_project_filter, state='readonly', width=18
        )
        self.work_project_combo.pack(side='left', padx=(6, 12))
        self.work_project_combo.bind('<<ComboboxSelected>>', self.refresh_work)
        ttk.Label(filters, text='关键词').pack(side='left')
        self.work_keyword = tk.StringVar()
        keyword_entry = ttk.Entry(filters, textvariable=self.work_keyword, width=22)
        keyword_entry.pack(side='left', padx=6)
        keyword_entry.bind('<Return>', self.refresh_work)
        keyword_entry.bind('<Escape>', lambda event: (self.work_keyword.set(''), self.refresh_work()))
        ttk.Button(filters, text='筛选', command=self.refresh_work).pack(side='left')
        ttk.Button(filters, text='清除筛选', command=self.clear_work_filters).pack(side='left', padx=6)
        actions = ttk.Frame(records_panel)
        actions.pack(fill='x', pady=(6, 0))
        ttk.Button(actions, text='今天', command=self.show_today).pack(side='left', padx=(0, 8))
        ttk.Button(actions, text='本周汇总', command=self.show_summary).pack(side='left')
        ttk.Button(actions, text='本月汇总', command=self.show_monthly_summary).pack(side='left', padx=8)
        ttk.Button(actions, text='当前查询范围汇总', command=self.show_query_summary).pack(side='left')
        self.mutation_button(actions, text='删除选中记录', command=self.delete_work).pack(side='right')
        self.mutation_button(actions, text='编辑选中记录', command=self.edit_selected_work).pack(side='right', padx=8)
        self.work_total = tk.StringVar()
        ttk.Label(records_panel, textvariable=self.work_total, style='Summary.TLabel', wraplength=760).pack(fill='x', pady=6)
        panes = ttk.Panedwindow(records_panel, orient='vertical')
        panes.pack(fill='both', expand=True)
        list_panel = ttk.Frame(panes, height=240)
        details_panel = ttk.LabelFrame(panes, text='完整工作内容（选中记录后查看，可滚动、复制）', padding=8)
        panes.add(list_panel, weight=4)
        panes.add(details_panel, weight=1)
        self.work_tree = self._tree(list_panel, [
            ('id', '序号', 60), ('date', '日期', 110), ('project', '项目', 130),
            ('description', '工作内容摘要', 300), ('hours', '工时 / h', 80)
        ])
        self.work_details = tk.Text(
            details_panel, height=5, wrap='word', state='disabled',
            font=('Microsoft YaHei UI', 10)
        )
        details_scroll = ttk.Scrollbar(details_panel, orient='vertical', command=self.work_details.yview)
        self.work_details.configure(yscrollcommand=details_scroll.set)
        details_scroll.pack(side='right', fill='y')
        self.work_details.pack(side='left', fill='both', expand=True)
        self.work_descriptions = {}
        self.work_tree.bind('<<TreeviewSelect>>', self.show_work_details)
        self.work_tree.bind('<Double-1>', self.edit_selected_work)

    def _build_todo_tab(self):
        form = ttk.LabelFrame(self.todo_tab, text='新增待办', padding=12)
        form.pack(fill='x')
        self.task = tk.StringVar()
        self.priority = tk.StringVar(value='中')
        self.due_date = tk.StringVar()
        for col, label in enumerate(['任务描述', '优先级', '截止日期（YYYY.MM.DD，可选）']):
            ttk.Label(form, text=label).grid(row=0, column=col, sticky='w')
        ttk.Entry(form, textvariable=self.task, width=50).grid(row=1, column=0, sticky='ew', padx=(0, 12), pady=6)
        ttk.Combobox(form, textvariable=self.priority, values=list(self.PRIORITIES), state='readonly', width=8).grid(row=1, column=1, padx=(0, 12))
        due_control, self.due_hour, self.due_minute = self.deadline_input(form, self.due_date)
        due_control.grid(row=1, column=2, padx=(0, 12), sticky='ew')
        self.mutation_button(form, text='添加待办', command=self.save_todo).grid(row=1, column=3)
        form.columnconfigure(0, weight=1)
        exports = ttk.Frame(self.todo_tab)
        exports.pack(fill='x', pady=(8, 0))
        ttk.Button(exports, text='导出全部待办 Markdown', command=self.export_todos).pack(side='left')
        self.mutation_button(exports, text='独立归档已结束任务', command=self.show_todo_archive).pack(side='left', padx=8)
        bar = ttk.Frame(self.todo_tab)
        bar.pack(fill='x', pady=(12, 0))
        # 启动默认只看未完成任务；已完成 / 已取消的收起，勾选后展示。
        self.show_completed = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text='显示已完成 / 已取消任务', variable=self.show_completed, command=self.refresh_todos).pack(side='left')
        ttk.Button(bar, text='刷新', command=self.refresh_todos).pack(side='left', padx=12)
        self.mutation_button(bar, text='标记完成', command=self.complete_selected).pack(side='right', padx=(8, 0))
        self.mutation_button(bar, text='标记取消', command=self.cancel_selected).pack(side='right', padx=(8, 0))
        self.mutation_button(bar, text='删除选中任务', command=self.delete_selected_todo).pack(side='right')
        self.mutation_button(bar, text='编辑选中任务', command=self.edit_selected_todo).pack(side='right', padx=8)
        # 待办筛选：优先级 + 关键词，配合上面的“显示已完成”开关一起生效。
        filters = ttk.Frame(self.todo_tab)
        filters.pack(fill='x', pady=(6, 0))
        ttk.Label(filters, text='优先级').pack(side='left')
        self.todo_priority_filter = tk.StringVar(value='全部')
        priority_combo = ttk.Combobox(
            filters, textvariable=self.todo_priority_filter, state='readonly', width=8,
            values=('全部',) + tuple(self.PRIORITIES)
        )
        priority_combo.pack(side='left', padx=6)
        priority_combo.bind('<<ComboboxSelected>>', self.refresh_todos)
        ttk.Label(filters, text='关键词').pack(side='left', padx=(12, 0))
        self.todo_keyword = tk.StringVar()
        todo_keyword_entry = ttk.Entry(filters, textvariable=self.todo_keyword, width=26)
        todo_keyword_entry.pack(side='left', padx=6)
        todo_keyword_entry.bind('<Return>', self.refresh_todos)
        todo_keyword_entry.bind('<Escape>', lambda event: (self.todo_keyword.set(''), self.refresh_todos()))
        ttk.Button(filters, text='清除筛选', command=self.clear_todo_filters).pack(side='left', padx=6)
        self.todo_tree = self._tree(self.todo_tab, [
            ('id', '序号', 60), ('task', '任务', 490), ('status', '状态', 100),
            ('priority', '优先级', 85), ('due', '截止时间', 170),
            ('reminder', '提醒状态', 230), ('reason', '取消原因', 200)
        ])
        self.todo_tree.tag_configure('overdue', foreground='#b42318')
        self.todo_tree.tag_configure('completed', foreground='#697586')
        self.todo_tree.tag_configure('cancelled', foreground='#697586')
        self.todo_tree.bind('<Double-1>', self.edit_selected_todo)
        self.todo_total = tk.StringVar()
        ttk.Label(self.todo_tab, textvariable=self.todo_total, style='Summary.TLabel').pack(anchor='w')
        ttk.Label(self.todo_tab, text='红色表示已过截止日期的未完成任务。').pack(anchor='w', pady=(5, 0))

    def _build_reminder_tab(self):
        """循环提醒 Tab：列表 + 新建/编辑弹窗入口。"""
        frame = ttk.Frame(self.reminder_tab)
        frame.pack(fill='both', expand=True)

        # 顶部工具栏
        bar = ttk.Frame(frame)
        bar.pack(fill='x')
        ttk.Button(bar, text='＋ 新建提醒', command=self.show_reminder_dialog).pack(side='left')
        ttk.Button(bar, text='✎ 编辑', command=self.edit_selected_reminder).pack(side='left', padx=4)
        ttk.Button(bar, text='🗑 删除', command=self.delete_selected_reminder).pack(side='left')
        ttk.Separator(bar, orient='vertical').pack(side='left', fill='y', padx=6)
        ttk.Button(bar, text='⏸ 暂停', command=lambda: self.toggle_selected_reminder(False)).pack(side='left', padx=(0, 4))
        ttk.Button(bar, text='▶ 启用', command=lambda: self.toggle_selected_reminder(True)).pack(side='left')
        ttk.Button(bar, text='立即刷新', command=self.refresh_reminders).pack(side='right')

        # 列表
        tree_frame = ttk.Frame(frame)
        tree_frame.pack(fill='both', expand=True, pady=(6, 4))
        self.reminder_tree = ttk.Treeview(
            tree_frame,
            columns=('title', 'rule', 'next_fire', 'time', 'status', 'count'),
            show='headings', selectmode='browse', height=12,
        )
        heads = [('title', '标题', 180), ('rule', '规则', 260),
                 ('next_fire', '下次触发', 100), ('time', '时间', 60),
                 ('status', '状态', 70), ('count', '次数', 60)]
        for col, text, w in heads:
            self.reminder_tree.heading(col, text=text)
            self.reminder_tree.column(col, width=w, anchor='w', stretch=(col in ('title', 'rule')))
        vsb = ttk.Scrollbar(tree_frame, orient='vertical', command=self.reminder_tree.yview)
        self.reminder_tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side='right', fill='y')
        self.reminder_tree.pack(side='left', fill='both', expand=True)
        self.reminder_tree.tag_configure('enabled', foreground='#166534')
        self.reminder_tree.tag_configure('disabled', foreground='#9ca3af')
        self.reminder_tree.tag_configure('overdue', foreground='#b91c1c')
        self.reminder_tree.bind('<Double-1>', lambda e: self.edit_selected_reminder())

        self.reminder_total = tk.StringVar()
        ttk.Label(frame, textvariable=self.reminder_total, style='Summary.TLabel').pack(anchor='w')

    def refresh_reminders(self):
        self.reminder_tree.delete(*self.reminder_tree.get_children())
        rows = self.tracker.list_reminders()
        today = date.today()
        for rid, title, rule, nxt, fire_time, enabled, skip, fired_count, last_fired in rows:
            try:
                rule_desc = self.tracker.describe_rule(rule) if isinstance(rule, str) else self.tracker.describe_rule(rule)
            except Exception:
                rule_desc = '(规则解析失败)'
            next_str = nxt.replace('-', '.') if nxt else '—'
            if nxt:
                try:
                    nxt_date = WorkTracker.parse_date_input(nxt)
                    tag = 'enabled' if enabled else 'disabled'
                    if enabled and nxt_date and nxt_date < today:
                        tag = 'overdue'
                except Exception:
                    tag = 'disabled'
            else:
                tag = 'disabled'
            status_text = '✔启用' if enabled else '⏸暂停'
            if not nxt and enabled:
                status_text = '✔已启用'  # countdown 过 target / once 已过 的 edge case
            self.reminder_tree.insert('', 'end', iid=str(rid),
                                      values=(title, rule_desc, next_str, fire_time or '09:00',
                                              status_text, fired_count),
                                      tags=(tag,))
        enabled_count = sum(1 for r in rows if r[5])
        self.reminder_total.set(f'共 {len(rows)} 条，其中启用 {enabled_count} 条')

    def _selected_reminder_id(self):
        sel = self.reminder_tree.selection()
        if not sel:
            return None
        try:
            return int(sel[0])
        except (ValueError, TypeError):
            return None

    def edit_selected_reminder(self):
        rid = self._selected_reminder_id()
        if rid is None:
            return
        cur = self.tracker.conn.execute(
            'SELECT title, rule, fire_time, enabled, weekend_skip FROM reminders WHERE id=?', (rid,))
        row = cur.fetchone()
        if row is None:
            return
        import json as _json
        try:
            rule_dict = _json.loads(row[1]) if isinstance(row[1], str) else row[1]
        except Exception:
            rule_dict = {'kind': 'yearly', 'month': 1, 'day': 1}
        self.show_reminder_dialog(
            record_id=rid, initial_title=row[0], initial_rule=rule_dict,
            initial_fire_time=row[2] or '09:00',
            initial_enabled=bool(row[3]), initial_weekend_skip=bool(row[4]),
        )

    def delete_selected_reminder(self):
        rid = self._selected_reminder_id()
        if rid is None:
            return
        title = self.reminder_tree.set(str(rid), 'title')
        if not tk.messagebox.askyesno('确认', f'确定删除提醒「{title}」吗？'):
            return
        try:
            self.tracker.delete_reminder(rid)
        except sqlite3.Error as exc:
            self.status.set(f'删除失败：{exc}')
            return
        self.refresh_reminders()

    def toggle_selected_reminder(self, enabled):
        rid = self._selected_reminder_id()
        if rid is None:
            return
        try:
            self.tracker.toggle_reminder(rid, enabled)
        except sqlite3.Error as exc:
            self.status.set(f'操作失败：{exc}')
            return
        self.refresh_reminders()

    def show_reminder_dialog(self, record_id=None, initial_title='', initial_rule=None,
                             initial_fire_time='09:00', initial_enabled=True,
                             initial_weekend_skip=False):
        """新建 / 编辑提醒弹窗。

        record_id=None → 新建；否则编辑。规则用"类型选择 → 动态展开参数"模式。
        """
        rule = dict(initial_rule) if initial_rule else {'kind': 'yearly', 'month': 1, 'day': 1}
        win = tk.Toplevel(self)
        win.title('编辑提醒事项' if record_id is not None else '新建提醒事项')
        win.geometry('460x480')
        win.resizable(False, False)
        win.transient(self)
        win.grab_set()

        body = ttk.Frame(win, padding=12)
        body.pack(fill='both', expand=True)

        # 标题
        ttk.Label(body, text='标题：').grid(row=0, column=0, sticky='w', pady=3)
        title_var = tk.StringVar(value=initial_title)
        ttk.Entry(body, textvariable=title_var, width=38).grid(row=0, column=1, sticky='ew', pady=3)

        # 规则类型
        ttk.Label(body, text='规则类型：').grid(row=1, column=0, sticky='w', pady=3)
        kind_var = tk.StringVar(value=rule.get('kind', 'yearly'))
        kinds = [
            ('每年（生日、纪念日）', 'yearly'),
            ('每月（每 N 月 X 号）', 'monthly'),
            ('每周 / 每 N 周的星期 X', 'weekly'),
            ('倒数（到某天前每天提醒）', 'countdown'),
            ('单次', 'once'),
        ]
        kind_names = {v: t for t, v in kinds}
        kind_display = tk.StringVar(value=kind_names.get(kind_var.get(), '每年（生日、纪念日）'))

        def _set_kind_display(*_):
            for text, v in kinds:
                if v == kind_var.get():
                    kind_display.set(text)
                    break

        kind_var.trace_add('write', _set_kind_display)
        kind_display.trace_add('write', lambda *_: None)  # 只展示，不反向
        # ttk.Combobox 不可变文本：用 Button + 下拉也行，这里直接用 Combobox 让用户选
        kind_combo = ttk.Combobox(
            body, values=[t for t, _ in kinds], state='readonly', width=36,
            textvariable=kind_display)
        # 把 display 的 text 映射回 value 麻烦，改成直接用 kind_var 绑定的下拉
        kind_combo.destroy()  # 丢弃，改用下面的 radiobutton 更清晰

        radio_frame = ttk.Frame(body)
        radio_frame.grid(row=1, column=1, sticky='w', pady=3)
        for i, (text, v) in enumerate(kinds):
            ttk.Radiobutton(radio_frame, text=text, value=v, variable=kind_var).grid(
                row=i // 2, column=i % 2, sticky='w', padx=2, pady=1)

        # 动态参数区
        params_outer = ttk.LabelFrame(body, text='具体参数', padding=8)
        params_outer.grid(row=2, column=0, columnspan=2, sticky='ew', pady=(8, 4))
        params_frame = ttk.Frame(params_outer)
        params_frame.pack(fill='x')

        # --- yearly 参数 ---
        yearly_frame = ttk.Frame(params_frame)
        ttk.Label(yearly_frame, text='月：').pack(side='left')
        yearly_month = tk.IntVar(value=rule.get('month', 1))
        yearly_day = tk.IntVar(value=rule.get('day', 1))
        ttk.Spinbox(yearly_frame, from_=1, to=12, width=5, textvariable=yearly_month).pack(side='left')
        ttk.Label(yearly_frame, text=' 日：').pack(side='left', padx=(2, 0))
        ttk.Spinbox(yearly_frame, from_=1, to=31, width=5, textvariable=yearly_day).pack(side='left')

        # --- monthly 参数 ---
        monthly_frame = ttk.Frame(params_frame)
        monthly_every = tk.IntVar(value=rule.get('every', 1))
        monthly_day = tk.IntVar(value=rule.get('day', 15))
        ttk.Label(monthly_frame, text='每 ').pack(side='left')
        ttk.Spinbox(monthly_frame, from_=1, to=24, width=4, textvariable=monthly_every).pack(side='left')
        ttk.Label(monthly_frame, text=' 个月的 ').pack(side='left')
        ttk.Spinbox(monthly_frame, from_=1, to=31, width=4, textvariable=monthly_day).pack(side='left')
        ttk.Label(monthly_frame, text=' 号').pack(side='left')

        # --- weekly 参数 ---
        weekly_frame = ttk.Frame(params_frame)
        weekly_every = tk.IntVar(value=rule.get('every', 1))
        raw_wd = rule.get('weekdays')
        if raw_wd is None:
            raw_wd = [rule.get('weekday', 0)]
        selected_wd = {int(w) for w in raw_wd if 0 <= int(w) <= 6}
        weekly_vars = {}
        ttk.Label(weekly_frame, text='每 ').pack(side='left')
        ttk.Spinbox(weekly_frame, from_=1, to=12, width=4, textvariable=weekly_every).pack(side='left')
        ttk.Label(weekly_frame, text=' 周的：').pack(side='left', padx=(2, 0))
        weekday_box = ttk.Frame(weekly_frame)
        weekday_box.pack(side='left')
        weekday_names = ['一', '二', '三', '四', '五', '六', '日']
        for i, name in enumerate(weekday_names):
            v = tk.BooleanVar(value=(i in selected_wd))
            weekly_vars[i] = v
            ttk.Checkbutton(weekday_box, text=name, variable=v).grid(row=0, column=i, padx=1)

        # --- countdown 参数 ---
        countdown_frame = ttk.Frame(params_frame)
        countdown_target = tk.StringVar(value=rule.get('target', date.today().isoformat().replace('-', '.')))
        ttk.Label(countdown_frame, text='目标日期（YYYY.MM.DD）：').pack(side='left')
        ttk.Entry(countdown_frame, textvariable=countdown_target, width=14).pack(side='left')

        # --- once 参数 ---
        once_frame = ttk.Frame(params_frame)
        once_date = tk.StringVar(value=rule.get('date', date.today().isoformat().replace('-', '.')))
        ttk.Label(once_frame, text='日期（YYYY.MM.DD）：').pack(side='left')
        ttk.Entry(once_frame, textvariable=once_date, width=14).pack(side='left')

        param_frames = {
            'yearly': yearly_frame, 'monthly': monthly_frame, 'weekly': weekly_frame,
            'countdown': countdown_frame, 'once': once_frame,
        }

        def _show_params():
            for f in param_frames.values():
                f.pack_forget()
            param_frames.get(kind_var.get(), yearly_frame).pack(fill='x')

        kind_var.trace_add('write', lambda *_: _show_params())
        _show_params()

        # 每天提醒时间
        time_row = ttk.Frame(body)
        time_row.grid(row=3, column=0, columnspan=2, sticky='w', pady=3)
        ttk.Label(time_row, text='每天提醒时间：').pack(side='left')
        fire_time = tk.StringVar(value=initial_fire_time)
        ttk.Entry(time_row, textvariable=fire_time, width=7).pack(side='left')
        ttk.Label(time_row, text=' （HH:MM，默认 09:00）').pack(side='left', padx=4)

        # 工作日除外
        skip_row = ttk.Frame(body)
        skip_row.grid(row=4, column=0, columnspan=2, sticky='w', pady=3)
        weekend_skip = tk.BooleanVar(value=initial_weekend_skip)
        ttk.Checkbutton(skip_row, text='工作日除外（周末顺延到周一）', variable=weekend_skip).pack(side='left')

        # 启用状态
        enable_row = ttk.Frame(body)
        enable_row.grid(row=5, column=0, columnspan=2, sticky='w', pady=3)
        enabled_var = tk.BooleanVar(value=initial_enabled)
        ttk.Checkbutton(enable_row, text='立即启用', variable=enabled_var).pack(side='left')

        # 按钮
        btns = ttk.Frame(body)
        btns.grid(row=6, column=0, columnspan=2, sticky='e', pady=(14, 0))

        def _collect_rule():
            """把动态控件里的值收集成 rule dict，同时做基本校验。"""
            k = kind_var.get()
            r = {'kind': k}
            if k == 'yearly':
                m, d = yearly_month.get(), yearly_day.get()
                if not (1 <= m <= 12 and 1 <= d <= 31):
                    raise ValueError(f'月/日 不合法：{m}/{d}')
                r.update(month=m, day=d)
            elif k == 'monthly':
                e, d = monthly_every.get(), monthly_day.get()
                if e < 1:
                    raise ValueError('间隔月数必须 ≥ 1')
                if not (1 <= d <= 31):
                    raise ValueError('日必须在 1~31 之间')
                r.update(every=e, day=d)
            elif k == 'weekly':
                wd = sorted([w for w, v in weekly_vars.items() if v.get()])
                if not wd:
                    raise ValueError('请至少勾选一个星期。')
                r.update(every=weekly_every.get(), weekdays=wd)
            elif k == 'countdown':
                d = WorkTracker.parse_date_input(countdown_target.get())
                if d is None:
                    raise ValueError('请填写有效的目标日期，格式 YYYY.MM.DD')
                r.update(target=d.isoformat())
            elif k == 'once':
                d = WorkTracker.parse_date_input(once_date.get())
                if d is None:
                    raise ValueError('请填写有效的日期，格式 YYYY.MM.DD')
                r.update(date=d.isoformat())
            return r

        def _on_save():
            t = title_var.get().strip()
            if not t:
                tk.messagebox.showerror('校验失败', '请填写标题。', parent=win)
                return
            try:
                r = _collect_rule()
            except (tk.TclError, ValueError) as exc:
                tk.messagebox.showerror('校验失败', str(exc), parent=win)
                return
            ft = fire_time.get().strip() or '09:00'
            try:
                # 时间格式校验 HH:MM
                datetime.strptime(ft, '%H:%M')
            except ValueError:
                tk.messagebox.showerror('校验失败', '时间格式应为 HH:MM，例如 09:00', parent=win)
                return
            try:
                if record_id is None:
                    self.tracker.add_reminder(t, r, ft, weekend_skip.get())
                else:
                    self.tracker.update_reminder(
                        record_id, title=t, rule=r, fire_time=ft,
                        weekend_skip=weekend_skip.get(), enabled=enabled_var.get())
            except (sqlite3.Error, ValueError) as exc:
                tk.messagebox.showerror('保存失败', str(exc), parent=win)
                return
            self.refresh_reminders()
            win.destroy()

        ttk.Button(btns, text='保存', command=_on_save).pack(side='right', padx=(6, 0))
        ttk.Button(btns, text='取消', command=win.destroy).pack(side='right')
        win.bind('<Escape>', lambda e: win.destroy())
        win.bind('<Return>', lambda e: _on_save())
        self.center_dialog(win)


    def _build_work_calendar(self, parent):
        panel = ttk.LabelFrame(parent, text='每日工时月历', padding=5)
        # Reserve the bottom calendar first; the existing entry form uses the rest.
        panel.pack(side='bottom', fill='x', padx=(0, 6), pady=(6, 0),
               before=parent.winfo_children()[0])
        self.work_calendar_month = date.today().replace(day=1)
        heading = ttk.Frame(panel)
        heading.pack(fill='x')
        ttk.Button(heading, text='‹', width=3,
                   command=lambda: self.move_work_calendar(-1)).pack(side='left')
        self.work_calendar_caption = tk.StringVar()
        ttk.Label(heading, textvariable=self.work_calendar_caption,
                  anchor='center').pack(side='left', fill='x', expand=True)
        ttk.Button(heading, text='本月', width=5,
                   command=self.reset_work_calendar).pack(side='left')
        ttk.Button(heading, text='›', width=3,
                   command=lambda: self.move_work_calendar(1)).pack(side='right')
        # 月历用固定宽度居中：不随左栏拉宽，格子紧贴文字，不再留大片空白。
        cal_wrap = ttk.Frame(panel)
        cal_wrap.pack(fill='x', pady=2)
        self.work_calendar_days = ttk.Frame(cal_wrap)
        self.work_calendar_days.pack(anchor='center')
        # 正方形格子：列宽与行高都锁到同一边长（像素），七列等宽不拉伸。
        self._cal_cell = 32
        for col in range(7):
            self.work_calendar_days.columnconfigure(
                col, weight=0, uniform='day', minsize=self._cal_cell)
        for col, label in enumerate(('一', '二', '三', '四', '五', '六', '日')):
            ttk.Label(self.work_calendar_days, text=label, anchor='center', width=2).grid(
                row=0, column=col, sticky='ew')
        # 42 个日期格子只创建一次，刷新时仅改文字与颜色，避免反复销毁重建控件。
        self._cal_empty_bg = (ttk.Style(self).lookup('TLabelframe', 'background')
                              or self.cget('background'))
        # 固定两行文字高度：空格子与有记录的格子行数一致，切月份时高度不再跳动。
        # 今天不用 ★ 之类字体外的符号，改用加粗字体 + 橙色边框，避免字体回退改变行高。
        # 32px 方格内要放两行字：7pt 行高 14px，两行正好 28px，留 4px 余量不裁字。
        self._cal_font = ('Microsoft YaHei UI', 7)
        self._cal_font_bold = ('Microsoft YaHei UI', 7, 'bold')
        self._calendar_days = {}
        self.work_calendar_cells = []
        for row in range(1, 7):
            for col in range(7):
                cell = tk.Button(self.work_calendar_days, text='', width=1, height=1, padx=0, pady=0,
                                 font=self._cal_font, borderwidth=0, highlightthickness=0,
                                 relief='flat',
                                 command=lambda r=row, c=col: self._click_calendar_cell(r, c))
                cell.grid(row=row, column=col, sticky='nsew')
                self.work_calendar_cells.append((row, col, cell))
        # 行高锁成与列宽相同的边长：格子恒为正方形，换月时尺寸不跳。
        for row in range(1, 7):
            self.work_calendar_days.rowconfigure(row, minsize=self._cal_cell)
        ttk.Label(panel, text='蓝：<8h   绿：≥8h\n数字下为工时',
                  justify='left').pack(anchor='w')

    def move_work_calendar(self, delta):
        month = self.work_calendar_month
        year, offset = divmod(month.year * 12 + month.month - 1 + delta, 12)
        if 1 <= year <= 9999:
            self.work_calendar_month = date(year, offset + 1, 1)
            self.refresh_work_calendar()

    def reset_work_calendar(self):
        self.work_calendar_month = date.today().replace(day=1)
        self.refresh_work_calendar()

    def select_work_calendar_day(self, day):
        self.query_range.set('自定义范围')
        self.query_date.set(self.display_date(day))
        self.query_end_date.set(self.display_date(day))
        self.change_work_range()

    def _click_calendar_cell(self, row, col):
        day = self._calendar_days.get((row, col))
        if day is not None:
            self.select_work_calendar_day(day)

    def refresh_work_calendar(self):
        """复用已创建的格子，仅在换月等必要场景更新文字与配色。"""
        if not hasattr(self, 'work_calendar_cells'):
            return
        month = self.work_calendar_month
        last_day = calendar.monthrange(month.year, month.month)[1]
        try:
            totals = dict(self.view_connection.execute(
                'SELECT date, SUM(hours) FROM work_logs WHERE date BETWEEN ? AND ? GROUP BY date',
                (month.isoformat(), month.replace(day=last_day).isoformat())
            ).fetchall())
        except sqlite3.Error:
            totals = {}
        self.work_calendar_caption.set(f'{month.year:04d}年{month.month:02d}月')
        weeks = calendar.monthcalendar(month.year, month.month)
        used_rows = len(weeks)
        weeks += [[0] * 7 for _ in range(6 - used_rows)]
        numbers = [number for week in weeks for number in week]
        today = date.today()
        self._calendar_days = {}
        for (row, col, cell), number in zip(self.work_calendar_cells, numbers):
            # 六行永远占位：5 周的月份最后一行留空，切月时左栏高度完全不变。
            cell.grid(row=row, column=col, sticky='nsew')
            if not number:
                # 补位空格与有内容的格子同尺寸（由 grid minsize 锁定），底色与面板一致即视觉空白。
                cell.configure(text='\n', state='disabled', relief='flat', borderwidth=0,
                               font=self._cal_font,
                               bg=self._cal_empty_bg, activebackground=self._cal_empty_bg,
                               highlightthickness=0)
                continue
            day = month.replace(day=number)
            self._calendar_days[(row, col)] = day
            hours = totals.get(day.isoformat(), 0)
            is_today = day == today
            if is_today:
                # 今天：整格暖橙底 + 深棕字 + 橙色粗边框，不管有没有工时都最醒目。
                background, foreground = "#EAF17B", '#9A3412'
            elif hours >= 8 - 1e-9:
                background, foreground = '#DCFCE7', '#166534'
            elif hours > 0:
                background, foreground = '#DBEAFE', '#1E40AF'
            else:
                background, foreground = '#F3F4F6', '#64748B'
            hours_text = f'{hours:g}h' if hours > 0 else '—'
            # 固定两行：第一行日期，第二行工时。
            # 今天不额外加字（格子窄，加字会挤掉），改用橙色粗边框 + 加粗来区分。
            text = f'{number}\n{hours_text}'
            # 今天用 2px 橙环标出；其余格子无边框，尺寸由 grid 的 32px 边长强制统一。
            cell.configure(
                text=text, state='normal', relief='flat', borderwidth=0,
                font=self._cal_font_bold if is_today else self._cal_font,
                bg=background, fg=foreground,
                activebackground=background, activeforeground=foreground,
                highlightbackground='#EA580C', highlightcolor='#EA580C',
                highlightthickness=2 if is_today else 0,
            )

    def show_work_details(self, event=None):
        selection = self.work_tree.selection()
        text = self.work_descriptions.get(selection[0], '') if selection else ''
        self.work_details.configure(state='normal')
        self.work_details.delete('1.0', 'end')
        self.work_details.insert('1.0', text)
        self.work_details.configure(state='disabled')
        self.work_details.yview_moveto(0)

    def change_work_range(self, event=None):
        custom = self.query_range.get() == '自定义范围'
        state = 'normal' if custom else 'disabled'
        self.query_start_entry.configure(state=state)
        self.query_end_entry.configure(state=state)
        if custom:
            # 从「全部记录」切过来时日期框是空的，先填今天，避免一上来就报错。
            if not self.query_date.get().strip() or not self.query_end_date.get().strip():
                today = date.today()
                self.query_date.set(self.display_date(today))
                self.query_end_date.set(self.display_date(today))
        else:
            # 快捷范围：把实际区间回显到日期框，用户能看到当前口径。
            start, end = self.work_query_bounds()
            if start and end:
                self.query_date.set(self.display_date(date.fromisoformat(start)))
                self.query_end_date.set(self.display_date(date.fromisoformat(end)))
            else:
                self.query_date.set('')
                self.query_end_date.set('')
        self.refresh_work()

    def work_query_bounds(self):
        """计算当前查询范围，纯读取不改界面；自定义范围含首尾两天。"""
        mode = self.query_range.get()
        today = date.today()
        if mode == '全部记录':
            return None, None
        if mode == '自定义范围':
            start = self.valid_date(self.query_date.get())
            end = self.valid_date(self.query_end_date.get())
            if start > end:
                raise ValueError('开始日期不能晚于结束日期。')
            return start, end
        if mode == '近七天':
            start, end = today - timedelta(days=6), today
        elif mode == '本月':
            start = today.replace(day=1)
            end = (start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        else:
            start = end = today
        return start.isoformat(), end.isoformat()

    def refresh_work(self):
        try:
            self.refresh_work_calendar()
            self.refresh_project_filter()
            start, end = self.work_query_bounds()
            clauses, params = [], []
            if start is not None:
                clauses.append('date BETWEEN ? AND ?')
                params.extend((start, end))
            project = self.work_project_filter.get()
            if project and project != '全部项目':
                clauses.append('project = ?')
                params.append(project)
            keyword = self.work_keyword.get().strip()
            if keyword:
                clauses.append('(description LIKE ? OR project LIKE ?)')
                params.extend((f'%{keyword}%', f'%{keyword}%'))
            sql = 'SELECT id, date, project, description, hours FROM work_logs'
            if clauses:
                sql += ' WHERE ' + ' AND '.join(clauses)
            # 列表最多渲染 2000 行：再往下用「缩小查询范围 / 用筛选」来找，
            # 避免几千行一次性灌进 Treeview 导致界面卡顿。
            rows = self.view_connection.execute(
                sql + ' ORDER BY date ASC, created_at ASC, id ASC LIMIT 2000', params
            ).fetchall()
            total_rows = self.view_connection.execute(
                'SELECT COUNT(*) FROM work_logs' + (' WHERE ' + ' AND '.join(clauses) if clauses else ''),
                params
            ).fetchone()[0]
            selection = self.work_tree.selection()
            self.work_descriptions = {str(row[0]): row[3] or '' for row in rows}
            self.work_tree.delete(*self.work_tree.get_children())
            for sequence, row in enumerate(rows, start=1):
                # 表格只显示单行摘要，原始内容在详情区完整展示，数据库不变。
                summary = ' / '.join((row[3] or '').splitlines())
                # 可见序号跟随查询排序；iid 仍使用数据库 ID，供删除和详情定位。
                self.work_tree.insert('', 'end', iid=str(row[0]), values=(sequence, self.display_date(row[1]), row[2], summary, row[4]))
            if rows:
                selected = selection[0] if selection and selection[0] in self.work_descriptions else str(rows[0][0])
                self.work_tree.selection_set(selected)
                self.work_tree.focus(selected)
                self.work_tree.see(selected)
            self.show_work_details()
            label = f'{self.display_date(start)} 至 {self.display_date(end)}' if start is not None else '全部历史记录'
            days = len({row[1] for row in rows})
            extra = ''
            if project and project != '全部项目':
                extra += f'  ·  项目「{project}」'
            if keyword:
                extra += f'  ·  关键词「{keyword}」'
            self.work_total.set(
                f'{label}  ·  {len(rows)} 条记录 / {days} 个记录日  ·  合计 {sum(row[4] for row in rows):g} 小时{extra}'
            )
            if total_rows > len(rows):
                self.work_total.set(
                    self.work_total.get() + f'  ·  仅显示前 {len(rows)} 条，共 {total_rows} 条，请缩小范围或用筛选'
                )
            self.refresh_overview()
        except (ValueError, sqlite3.Error) as exc:
            self.show_message('查询失败', str(exc), parent=self)

    def refresh_project_filter(self):
        """把数据库里出现过的项目填进下拉框，保留用户当前选择。"""
        try:
            names = [row[0] for row in self.view_connection.execute(
                "SELECT DISTINCT project FROM work_logs WHERE project IS NOT NULL AND TRIM(project) <> '' "
                'ORDER BY project'
            ).fetchall()]
        except sqlite3.Error:
            return
        current = self.work_project_filter.get()
        values = ['全部项目'] + names
        self.work_project_combo.configure(values=values)
        if current not in values:
            self.work_project_filter.set('全部项目')

    def clear_work_filters(self):
        self.work_project_filter.set('全部项目')
        self.work_keyword.set('')
        self.refresh_work()
        self.status.set('已清除筛选条件')

    def show_today(self):
        self.query_range.set('今天')
        # 同步新增工时的默认日期，避免切到“今天”后仍沿用旧日期。
        self.log_date.set(self.display_date(date.today()))
        self.change_work_range()

    def copy_previous_work(self):
        """把最近一个有记录的日期的工时复制到录入表单，减少重复打字。"""
        if not self.can_edit_current():
            return
        try:
            row = self.view_connection.execute(
                'SELECT date, project, description, hours FROM work_logs '
                'ORDER BY date DESC, created_at DESC, id DESC LIMIT 1'
            ).fetchone()
        except sqlite3.Error as exc:
            self.show_message('读取失败', str(exc), parent=self)
            return
        if row is None:
            self.show_message('提示', '当前数据来源还没有任何工时记录。', parent=self)
            return
        source_day, project, description, hours = row
        self.log_date.set(self.display_date(date.today()))
        self.project.set(project or '')
        self.hours.set(f'{hours:g}')
        self.description.delete('1.0', 'end')
        if description:
            self.description.insert('1.0', description)
        self.status.set(f'已复制 {self.display_date(date.fromisoformat(source_day))} 的记录，请确认后保存')
        self.description.focus_set()

    def save_work(self):
        if not self.can_edit_current():
            return
        try:
            day = self.valid_date(self.log_date.get())
            project = self.project.get().strip()
            # 仅排除 Text 控件自动附加的末尾换行，正文不做日期转换或裁剪。
            description = self.description.get('1.0', 'end-1c')
            if not project or not description.strip():
                raise ValueError('请填写项目名称和工作内容。')
            try:
                hours = float(self.hours.get())
            except ValueError:
                raise ValueError('工时请输入数字。') from None
            if not math.isfinite(hours) or not 0 < hours <= 24:
                raise ValueError('单条工时必须大于 0 且不超过 24 小时。')
            self.tracker.add_work_log(project, description, hours, day)
            daily_hours = self.tracker.conn.execute(
                'SELECT COALESCE(SUM(hours), 0) FROM work_logs WHERE date=?', (day,)
            ).fetchone()[0]
            # 保存后定位到录入当天，让补录的历史记录也能立即看到。
            self.query_range.set('自定义范围')
            self.log_date.set(self.display_date(day))
            self.query_date.set(self.display_date(day))
            self.query_end_date.set(self.display_date(day))
            self.hours.set('')
            self.description.delete('1.0', 'end')
            self.change_work_range()
            self.status.set('工时已保存')
            if daily_hours > 8 + 1e-9:
                day_label = '今天' if day == date.today().isoformat() else f'{self.display_date(day)} 当天'
                self.show_message(
                    '注意休息',
                    f'工时已保存。\n\n{day_label}的工时超过了 8 h，已累计 {daily_hours:g} h。\n要注意休息！',
                    parent=self
                )
        except (ValueError, sqlite3.Error) as exc:
            self.show_message('保存失败', str(exc), parent=self)

    def _selected_id(self, tree):
        selection = tree.selection()
        if not selection:
            self.show_message('提示', '请先在列表中选中一条记录。', parent=self)
            return None
        return int(selection[0])

    def edit_selected_work(self, event=None):
        if not self.can_edit_current():
            return
        if event is not None:
            if self.work_tree.identify_region(event.x, event.y) != 'cell':
                return
            row_id = self.work_tree.identify_row(event.y)
            if not row_id:
                return
            self.work_tree.selection_set(row_id)
        record_id = self._selected_id(self.work_tree)
        if record_id is None:
            return
        try:
            row = self.tracker.conn.execute(
                'SELECT date, project, description, hours FROM work_logs WHERE id=?', (record_id,)
            ).fetchone()
        except sqlite3.Error as exc:
            self.show_message('读取失败', str(exc), parent=self)
            return
        if row is None:
            self.show_message('提示', '该工时记录已不存在，请刷新列表。', parent=self)
            self.refresh_work()
            return
        window = tk.Toplevel(self)
        window.title(f'编辑工时 · 编号 {record_id}')
        window.transient(self)
        content = ttk.Frame(window, padding=18)
        content.pack(fill='both', expand=True)
        content.columnconfigure(1, weight=1)
        content.rowconfigure(3, weight=1)
        day = tk.StringVar(window, value=self.display_date(row[0]))
        project = tk.StringVar(window, value=row[1])
        hours = tk.StringVar(window, value=str(row[3]))
        for index, label in enumerate(('工作日期', '项目名称', '工时（小时）', '工作内容')):
            ttk.Label(content, text=label).grid(row=index, column=0, sticky='nw', padx=(0, 12), pady=8)
        self.date_input(content, day).grid(row=0, column=1, sticky='ew')
        project_entry = ttk.Entry(content, textvariable=project)
        project_entry.grid(row=1, column=1, sticky='ew')
        ttk.Entry(content, textvariable=hours).grid(row=2, column=1, sticky='ew')
        text_panel = ttk.Frame(content)
        text_panel.grid(row=3, column=1, sticky='nsew')
        description = tk.Text(text_panel, width=50, height=8, wrap='word', undo=True,
                              font=('Microsoft YaHei UI', 10))
        scrollbar = ttk.Scrollbar(text_panel, orient='vertical', command=description.yview)
        description.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side='right', fill='y')
        description.pack(fill='both', expand=True)
        description.insert('1.0', row[2] or '')

        def save_changes():
            if not self.can_edit_current():
                return
            try:
                canonical_day = self.valid_date(day.get())
                project_name = project.get().strip()
                body = description.get('1.0', 'end-1c')
                if not project_name or not body.strip():
                    raise ValueError('请填写项目名称和工作内容。')
                try:
                    duration = float(hours.get())
                except ValueError:
                    raise ValueError('工时请输入数字。') from None
                if not math.isfinite(duration) or not 0 < duration <= 24:
                    raise ValueError('单条工时必须大于 0 且不超过 24 小时。')
                with self.tracker.conn:
                    cursor = self.tracker.conn.execute(
                        'UPDATE work_logs SET date=?, project=?, description=?, hours=? WHERE id=?',
                        (canonical_day, project_name, body, duration, record_id)
                    )
                    if cursor.rowcount != 1:
                        raise ValueError('该记录已不存在，请刷新列表。')
            except (ValueError, sqlite3.Error) as exc:
                self.show_message('保存失败', str(exc), parent=window)
                return
            window.destroy()
            # Editing must not replace the user's current query range/dates.
            self.refresh_work()
            if self.work_tree.exists(str(record_id)):
                self.work_tree.selection_set(str(record_id))
                self.work_tree.focus(str(record_id))
                self.work_tree.see(str(record_id))
                self.show_work_details()
                self.status.set(f'工时 [{record_id}] 已更新')
            else:
                self.status.set(f'工时 [{record_id}] 已更新；修改后的日期不在当前查询范围内')

        buttons = ttk.Frame(content)
        buttons.grid(row=4, column=0, columnspan=2, sticky='e', pady=(12, 0))
        ttk.Button(buttons, text='保存修改', command=save_changes).pack(side='left')
        ttk.Button(buttons, text='取消', command=window.destroy).pack(side='left', padx=(8, 0))
        window.bind('<Escape>', lambda event: window.destroy())
        window.update_idletasks()
        window.minsize(window.winfo_reqwidth(), window.winfo_reqheight())
        self.center_dialog(window)
        window.grab_set()
        project_entry.focus_set()

    def delete_work(self):
        if not self.can_edit_current():
            return
        record_id = self._selected_id(self.work_tree)
        if record_id is None or not self.show_message('确认删除', '删除选中的工时记录？\n删除后 10 秒内可在底部状态栏撤销。', parent=self, confirm=True):
            return
        try:
            snapshot = self.tracker.conn.execute(
                'SELECT id, date, project, description, hours, created_at FROM work_logs WHERE id=?',
                (record_id,)
            ).fetchone()
            with self.tracker.conn:
                self.tracker.conn.execute('DELETE FROM work_logs WHERE id=?', (record_id,))
            self.refresh_work()
            if snapshot is not None:
                self._offer_undo('work', snapshot, f'工时记录 [{record_id}] 已删除')
            else:
                self.status.set('工时记录已删除')
        except sqlite3.Error as exc:
            self.show_message('删除失败', str(exc), parent=self)

    # ==================== 撤销删除 ====================

    def _offer_undo(self, kind, snapshot, label, timeout_ms=10000):
        """缓存被删整行，在状态栏给出限时撤销入口；超时或再次删除时自动失效。"""
        self._cancel_undo()
        self._undo_state = (kind, snapshot)
        self._undo_button.pack(side='left', padx=(12, 0))
        self._undo_job = self.after(timeout_ms, self._cancel_undo)
        self.status.set(f'{label} · {timeout_ms // 1000} 秒内可撤销')

    def _cancel_undo(self):
        if self._undo_job is not None:
            self.after_cancel(self._undo_job)
            self._undo_job = None
        self._undo_state = None
        self._undo_button.pack_forget()

    def _undo_delete(self):
        state = self._undo_state
        self._cancel_undo()
        if state is None or not self.can_edit_current():
            return
        kind, row = state
        try:
            if kind == 'work':
                with self.tracker.conn:
                    self.tracker.conn.execute(
                        'INSERT INTO work_logs (id, date, project, description, hours, created_at) '
                        'VALUES (?, ?, ?, ?, ?, ?)', row
                    )
                self.refresh_work()
                self.status.set(f'已撤销删除，工时记录 [{row[0]}] 已恢复')
            else:
                with self.tracker.conn:
                    self.tracker.conn.execute(
                        'INSERT INTO todos (id, task, status, priority, due_date, created_at, '
                        'completed_at, cancel_reason, reminder_enabled, reminder_snooze_until, '
                        'reminder_last_day) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', row
                    )
                self.refresh_todos()
                self.status.set(f'已撤销删除，待办 [{row[0]}] 已恢复')
        except sqlite3.Error as exc:
            self.show_message('撤销失败', f'记录未能恢复：{exc}', parent=self)

    def show_summary(self):
        today = date.today()
        start = today - timedelta(days=today.weekday())
        end = start + timedelta(days=6)
        self._show_period_summary(start, end, '本周工时汇总（周一至周日）')

    def show_monthly_summary(self):
        today = date.today()
        start = today.replace(day=1)
        next_month = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
        end = next_month - timedelta(days=1)
        self._show_period_summary(start, end, f'{today:%Y年%m月}工时汇总')

    def _show_period_summary(self, start, end, title):
        try:
            rows = self.view_connection.execute(
                'SELECT date, SUM(hours), GROUP_CONCAT(DISTINCT project) FROM work_logs '
                'WHERE date BETWEEN ? AND ? GROUP BY date ORDER BY date DESC',
                (start.isoformat(), end.isoformat())
            ).fetchall()
            project_totals = self.view_connection.execute(
                "SELECT COALESCE(NULLIF(TRIM(project), ''), '未命名项目') AS name, SUM(hours) "
                'FROM work_logs WHERE date BETWEEN ? AND ? '
                'GROUP BY name ORDER BY SUM(hours) DESC, name ASC',
                (start.isoformat(), end.isoformat())
            ).fetchall()
        except sqlite3.Error as exc:
            self.show_message('查询失败', str(exc), parent=self)
            return
        window = tk.Toplevel(self)
        window.title(f'{title} · {self.source_hint.get()}')
        window.geometry('900x650')
        window.minsize(680, 480)
        window.transient(self)
        content = ttk.Frame(window, padding=16)
        content.pack(fill='both', expand=True)
        ttk.Label(content, text=f'{self.display_date(start)} 至 {self.display_date(end)}  ·  合计 {sum(r[1] for r in rows):g} 小时', style='Summary.TLabel').pack(anchor='w')
        tabs = ttk.Notebook(content)
        tabs.pack(fill='both', expand=True, pady=(12, 0))
        chart_panel = ttk.Frame(tabs, padding=10)
        daily_panel = ttk.Frame(tabs, padding=8)
        tabs.add(chart_panel, text='  项目工时占比  ')
        tabs.add(daily_panel, text='  每日汇总明细  ')
        ttk.Label(chart_panel, text='按项目累计工时排序；占比 = 项目工时 ÷ 当前汇总范围总工时').pack(anchor='w', pady=(0, 8))
        chart_frame = ttk.Frame(chart_panel)
        chart_frame.pack(fill='both', expand=True)
        chart = tk.Canvas(chart_frame, background='#ffffff', highlightthickness=0, width=600, height=350)
        scroll = ttk.Scrollbar(chart_frame, orient='vertical', command=chart.yview)
        chart.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        chart.pack(side='left', fill='both', expand=True)
        total = sum(hours for _, hours in project_totals)
        colors = ('#2563eb', '#059669', '#d97706', '#7c3aed', '#0891b2', '#db2777')

        def draw_chart(event=None):
            chart.delete('all')
            width = max(chart.winfo_width(), 400)
            height = max(chart.winfo_height(), len(project_totals) * 78 + 20)
            chart.configure(scrollregion=(0, 0, width, height))
            if not project_totals or total <= 0:
                chart.create_text(width / 2, 60, text='此时间范围内暂无可统计的工时。', fill='#697586', font=('Microsoft YaHei UI', 11))
                return
            for index, (name, hours) in enumerate(project_totals):
                y = 16 + index * 78
                ratio = hours / total
                chart.create_text(16, y, text=name, anchor='nw', width=max(100, width - 210),
                                  font=('Microsoft YaHei UI', 10), fill='#1f2937')
                chart.create_text(width - 16, y, text=f'{hours:g} 小时  |  {ratio:.1%}', anchor='ne',
                                  font=('Microsoft YaHei UI', 10, 'bold'), fill='#1f2937')
                chart.create_rectangle(16, y + 44, width - 16, y + 60, fill='#eef2f6', outline='')
                chart.create_rectangle(16, y + 44, 16 + (width - 32) * ratio, y + 60,
                                       fill=colors[index % len(colors)], outline='')

        chart.bind('<Configure>', draw_chart)
        chart.bind('<MouseWheel>', lambda event: chart.yview_scroll(-int(event.delta / 120), 'units'))
        tree = self._tree(daily_panel, [('date', '日期', 130), ('hours', '工时 / h', 100), ('projects', '项目', 440)])
        for day, hours, daily_projects in rows:
            tree.insert('', 'end', values=(self.display_date(day), f'{hours:g}', daily_projects or ''))
        if not rows:
            ttk.Label(daily_panel, text='此时间范围内暂无工时记录。').pack(anchor='w')
        self.center_dialog(window)

    def save_todo(self):
        if not self.can_edit_current():
            return
        try:
            task = self.task.get().strip()
            if not task:
                raise ValueError('请填写任务描述。')
            due = self.deadline_value(self.due_date, self.due_hour, self.due_minute)
            self.tracker.add_todo(task, self.PRIORITIES[self.priority.get()], due)
            self.task.set('')
            self.due_date.set('')
            self.due_hour.set('')
            self.due_minute.set('')
            self.refresh_todos()
            self.status.set('待办已添加')
        except (ValueError, sqlite3.Error) as exc:
            self.show_message('添加失败', str(exc), parent=self)

    def refresh_todos(self):
        self._reminders_dirty = True
        try:
            clauses = []
            if not self.show_completed.get():
                clauses.append("status='pending'")
            priority = self.todo_priority_filter.get()
            if priority and priority != '全部':
                clauses.append('priority = ?')
            keyword = self.todo_keyword.get().strip()
            if keyword:
                clauses.append('task LIKE ?')
            sql = ('SELECT id, task, status, priority, due_date, cancel_reason, '
                   'reminder_enabled, reminder_snooze_until, reminder_last_day FROM todos')
            params = []
            if clauses:
                sql += ' WHERE ' + ' AND '.join(clauses)
            if priority and priority != '全部':
                params.append(self.PRIORITIES[priority])
            if keyword:
                params.append(f'%{keyword}%')
            rows = self.view_connection.execute(
                sql + ' ORDER BY created_at ASC, id ASC LIMIT 2000', params
            ).fetchall()
            total_rows = self.view_connection.execute(
                'SELECT COUNT(*) FROM todos' + (' WHERE ' + ' AND '.join(clauses) if clauses else ''),
                params
            ).fetchone()[0]
            self.todo_tree.delete(*self.todo_tree.get_children())
            names = {value: key for key, value in self.PRIORITIES.items()}
            status_names = {value: key for key, value in self.STATUSES.items()}
            # 一行只取一次时钟：避免每行重复调用系统时间函数。
            now = datetime.now()
            today = now.date()
            for sequence, (record_id, task, status, priority, due, reason, enabled, snooze, last_day) in enumerate(rows, start=1):
                try:
                    normalized_due = self.parse_deadline(due) if due else None
                except ValueError:
                    normalized_due = None
                overdue = normalized_due and (normalized_due < now if self.has_deadline_time(due) else normalized_due.date() < today)
                tags = (status,) if status in ('completed', 'cancelled') else ('overdue',) if status == 'pending' and overdue else ()
                self.todo_tree.insert('', 'end', iid=str(record_id), values=(
                    sequence, task, status_names.get(status, status), names.get(priority, priority), self.display_deadline(due) if due else '—',
                    self.reminder_status(status, due, enabled, snooze, last_day), reason or ''
                ), tags=tags)
            counts = dict(self.view_connection.execute('SELECT status, COUNT(*) FROM todos GROUP BY status').fetchall())
            extra = ''
            if priority and priority != '全部':
                extra += f'  ·  优先级「{priority}」'
            if keyword:
                extra += f'  ·  关键词「{keyword}」'
            self.todo_total.set(f"待处理 {counts.get('pending', 0)} 项  ·  已完成 {counts.get('completed', 0)} 项  ·  已取消 {counts.get('cancelled', 0)} 项  ·  当前显示 {len(rows)} 项{extra}")
            if total_rows > len(rows):
                self.todo_total.set(
                    self.todo_total.get() + f'  ·  仅显示前 {len(rows)} 条，共 {total_rows} 条，请用筛选缩小范围'
                )
            self.refresh_overview()
        except sqlite3.Error as exc:
            self.show_message('查询失败', str(exc), parent=self)

    def clear_todo_filters(self):
        self.todo_priority_filter.set('全部')
        self.todo_keyword.set('')
        self.refresh_todos()
        self.status.set('已清除筛选条件')

    def _grab_path(self):
        """返回原生 Tk 抓取路径，不转换 Combobox 的内部 popdown 控件。"""
        return str(self.tk.call('grab', 'current', self._w))

    def _python_widget(self, path):
        if not path:
            return None
        try:
            return self.nametowidget(str(path))
        except (KeyError, tk.TclError):
            # Tk 创建的内部窗口不一定注册在 Python children 中。
            return None

    def show_message(self, title, message, parent=None, confirm=False):
        """统一可定位的模态提示框，删除确认默认聚焦取消。"""
        parent = parent if parent is not None else self
        previous_grab = self._python_widget(self._grab_path())
        previous_focus = self._python_widget(self.tk.call('focus'))
        window = tk.Toplevel(parent)
        window.title(title)
        window.transient(parent)
        window.resizable(False, False)
        body = ttk.Frame(window, padding=20)
        body.pack(fill='both', expand=True)
        ttk.Label(body, text=message, wraplength=480, justify='left').pack(fill='x', pady=(0, 18))
        buttons = ttk.Frame(body)
        buttons.pack(fill='x')
        result = False

        def finish(accepted=False):
            nonlocal result
            result = accepted
            window.destroy()

        if confirm:
            cancel = ttk.Button(buttons, text='取消', command=finish)
            cancel.pack(side='right', padx=(8, 0))
            ttk.Button(buttons, text='确认', command=lambda: finish(True)).pack(side='right')
            focus_button = cancel
        else:
            focus_button = ttk.Button(buttons, text='确定', command=lambda: finish(True))
            focus_button.pack(side='right')
        window.protocol('WM_DELETE_WINDOW', finish)
        window.bind('<Escape>', lambda event: finish())
        self.center_dialog(window, parent)
        window.grab_set()
        focus_button.focus_set()
        try:
            self.wait_window(window)
        finally:
            if previous_grab is not None and previous_grab.winfo_exists():
                previous_grab.grab_set()
            if previous_focus is not None and previous_focus.winfo_exists():
                previous_focus.focus_set()
        return result

    def center_dialog(self, window, parent=None, corner=False):
        """在所属窗口所在显示器的工作区居中，支持副屏负坐标。"""
        parent = parent if parent is not None else self
        window.update_idletasks()
        if os.name == 'nt':
            import ctypes
            from ctypes import wintypes

            class MonitorInfo(ctypes.Structure):
                _fields_ = [('cbSize', wintypes.DWORD), ('rcMonitor', wintypes.RECT),
                            ('rcWork', wintypes.RECT), ('dwFlags', wintypes.DWORD)]

            user32 = ctypes.WinDLL('user32', use_last_error=True)
            user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
            user32.GetAncestor.restype = wintypes.HWND
            user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
            user32.MonitorFromWindow.restype = wintypes.HANDLE
            user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MonitorInfo)]
            user32.GetMonitorInfoW.restype = wintypes.BOOL
            user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
            user32.GetWindowRect.restype = wintypes.BOOL
            user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                          ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
            user32.SetWindowPos.restype = wintypes.BOOL
            parent_hwnd = user32.GetAncestor(parent.winfo_id(), 2)
            dialog_hwnd = user32.GetAncestor(window.winfo_id(), 2)
            monitor = user32.MonitorFromWindow(parent_hwnd, 2)
            info = MonitorInfo()
            info.cbSize = ctypes.sizeof(info)
            rect = wintypes.RECT()
            if user32.GetMonitorInfoW(monitor, ctypes.byref(info)) and user32.GetWindowRect(dialog_hwnd, ctypes.byref(rect)):
                work = info.rcWork
                x = work.left + max(0, (work.right - work.left - (rect.right - rect.left)) // 2)
                y = work.top + max(0, (work.bottom - work.top - (rect.bottom - rect.top)) // 2)
                if corner:
                    x = max(work.left, work.right - (rect.right - rect.left) - 16)
                    y = max(work.top, work.bottom - (rect.bottom - rect.top) - 16)
                # 使用原生坐标，避免 Tk 将负坐标解释为相对屏幕右/下边缘。
                if user32.SetWindowPos(dialog_hwnd, None, x, y, 0, 0, 0x0015):
                    return
        x = parent.winfo_rootx() + (parent.winfo_width() - window.winfo_width()) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - window.winfo_height()) // 2
        window.geometry(f'+{max(0, x)}+{max(0, y)}')

    def edit_selected_todo(self, event=None):
        if not self.can_edit_current():
            return
        if event is not None:
            if self.todo_tree.identify_region(event.x, event.y) != 'cell':
                return
            row_id = self.todo_tree.identify_row(event.y)
            if not row_id:
                return
            self.todo_tree.selection_set(row_id)
        record_id = self._selected_id(self.todo_tree)
        if record_id is None:
            return
        try:
            row = self.tracker.conn.execute(
                'SELECT task, priority, due_date, status, cancel_reason, completed_at, reminder_enabled FROM todos WHERE id=?', (record_id,)
            ).fetchone()
        except sqlite3.Error as exc:
            self.show_message('读取失败', str(exc), parent=self)
            return
        if row is None:
            self.show_message('提示', '该任务已不存在，请刷新列表。', parent=self)
            self.refresh_todos()
            return

        window = tk.Toplevel(self)
        window.title(f'编辑待办 · 编号 {record_id}')
        window.resizable(True, True)
        window.transient(self)
        content = ttk.Frame(window, padding=18)
        content.pack(fill='both', expand=True)
        content.columnconfigure(1, weight=1)
        task = tk.StringVar(window, value=row[0])
        names = {value: key for key, value in self.PRIORITIES.items()}
        priority = tk.StringVar(window, value=names.get(row[1], '中'))
        due = tk.StringVar(window, value=self.display_deadline(row[2]).split(' ')[0])
        due.trace_add('write', lambda *args: self.normalize_date_variable(due))
        status_names = {value: key for key, value in self.STATUSES.items()}
        status = tk.StringVar(window, value=status_names.get(row[3], '❓待处理'))
        reason = tk.StringVar(window, value=row[4] or '')
        ttk.Label(content, text='任务描述').grid(row=0, column=0, sticky='w', padx=(0, 12), pady=8)
        task_entry = ttk.Entry(content, textvariable=task)
        task_entry.grid(row=0, column=1, sticky='ew')
        ttk.Label(content, text='优先级').grid(row=1, column=0, sticky='w', pady=8)
        ttk.Combobox(content, textvariable=priority, values=list(self.PRIORITIES), state='readonly').grid(row=1, column=1, sticky='ew')
        ttk.Label(content, text='截止日期').grid(row=2, column=0, sticky='w', pady=8)
        due_entry, due_hour, due_minute = self.deadline_input(content, due, row[2])
        due_entry.grid(row=2, column=1, sticky='ew')
        ttk.Label(content, text='日期格式：YYYY.MM.DD；留空可取消截止日期。').grid(row=3, column=0, columnspan=2, sticky='w', pady=6)
        ttk.Label(content, text='任务状态').grid(row=4, column=0, sticky='w', pady=8)
        ttk.Combobox(content, textvariable=status, values=list(self.STATUSES), state='readonly').grid(row=4, column=1, sticky='ew')
        ttk.Label(content, text='取消原因（可选）').grid(row=5, column=0, sticky='w', pady=8)
        reason_entry = ttk.Entry(content, textvariable=reason)
        reason_entry.grid(row=5, column=1, sticky='ew')

        def update_reason_state(*args):
            reason_entry.configure(state='normal' if self.STATUSES.get(status.get()) == 'cancelled' else 'disabled')

        status.trace_add('write', update_reason_state)
        update_reason_state()
        reminder_enabled = tk.BooleanVar(window, value=bool(row[6]))
        ttk.Checkbutton(content, text='启用到期提醒（提前一天、当天和逾期）', variable=reminder_enabled).grid(
            row=6, column=0, columnspan=2, sticky='w', pady=8
        )
        reset_reminder = tk.BooleanVar(window, value=False)
        ttk.Checkbutton(content, text='清除今日确认 / 稍后提醒记录，重新检查提醒', variable=reset_reminder).grid(
            row=7, column=0, columnspan=2, sticky='w', pady=4
        )

        def save_changes():
            try:
                text = task.get().strip()
                if not text:
                    raise ValueError('请填写任务描述。')
                due_date = self.deadline_value(due, due_hour, due_minute)
                new_status = self.STATUSES[status.get()]
                completed_at = (row[5] if row[3] == 'completed' and row[5] else datetime.now().isoformat()) if new_status == 'completed' else None
                cancel_reason = reason.get().strip() if new_status == 'cancelled' else ''
                with self.tracker.conn:
                    result = self.tracker.conn.execute(
                        'UPDATE todos SET task=?, priority=?, due_date=?, status=?, cancel_reason=?, completed_at=? WHERE id=?',
                        (text, self.PRIORITIES[priority.get()], due_date, new_status, cancel_reason, completed_at, record_id)
                    )
                    if result.rowcount == 0:
                        raise ValueError('该任务已不存在，无法保存。')
                    reset = reset_reminder.get() or due_date != row[2] or new_status != row[3] or bool(row[6]) != reminder_enabled.get()
                    self.tracker.conn.execute(
                        'UPDATE todos SET reminder_enabled=?, '
                        'reminder_snooze_until=CASE WHEN ? THEN NULL ELSE reminder_snooze_until END, '
                        'reminder_last_day=CASE WHEN ? THEN NULL ELSE reminder_last_day END WHERE id=?',
                        (int(reminder_enabled.get()), reset, reset, record_id)
                    )
            except (ValueError, sqlite3.Error) as exc:
                self.show_message('保存失败', str(exc), parent=window)
                return
            window.destroy()
            self.refresh_todos()
            if self.todo_tree.exists(str(record_id)):
                self.todo_tree.selection_set(str(record_id))
                self.todo_tree.focus(str(record_id))
                self.todo_tree.see(str(record_id))
            self.status.set(f'待办 [{record_id}] 已更新')

        buttons = ttk.Frame(content)
        buttons.grid(row=8, column=0, columnspan=2, sticky='e', pady=(10, 0))
        ttk.Button(buttons, text='取消', command=window.destroy).pack(side='right', padx=(8, 0))
        ttk.Button(buttons, text='保存修改', command=save_changes).pack(side='right')
        window.bind('<Escape>', lambda event: window.destroy())
        window.update_idletasks()
        window.minsize(window.winfo_reqwidth(), window.winfo_reqheight())
        self.center_dialog(window)
        window.grab_set()
        task_entry.focus_set()

    def cancel_selected(self):
        if not self.can_edit_current():
            return
        record_id = self._selected_id(self.todo_tree)
        if record_id is None:
            return
        try:
            row = self.tracker.conn.execute(
                'SELECT task, status FROM todos WHERE id=?', (record_id,)
            ).fetchone()
            if row is None:
                self.show_message('提示', '该任务已不存在，请刷新列表。', parent=self)
                self.refresh_todos()
                return
            if row[1] == 'cancelled':
                self.show_message('提示', '该任务已经是已取消状态。', parent=self)
                return
            if not self.show_message(
                '确认取消任务', f'将以下任务标记为“❌已取消”？\n\n{row[0]}\n\n记录会保留，取消原因可在编辑窗口填写。',
                parent=self, confirm=True
            ):
                return
            with self.tracker.conn:
                self.tracker.conn.execute(
                    "UPDATE todos SET status='cancelled', completed_at=NULL WHERE id=?", (record_id,)
                )
            self.refresh_todos()
            if self.todo_tree.exists(str(record_id)):
                self.todo_tree.selection_set(str(record_id))
                self.todo_tree.focus(str(record_id))
                self.todo_tree.see(str(record_id))
            self.status.set('任务已标记取消，记录已保留')
        except sqlite3.Error as exc:
            self.show_message('操作失败', str(exc), parent=self)

    def complete_selected(self):
        if not self.can_edit_current():
            return
        record_id = self._selected_id(self.todo_tree)
        if record_id is None:
            return
        try:
            state = self.tracker.conn.execute('SELECT status FROM todos WHERE id=?', (record_id,)).fetchone()
            if state and state[0] == 'cancelled':
                self.show_message('提示', '该任务已取消，请先在编辑窗口恢复为待处理。', parent=self)
                return
            if state and state[0] == 'completed':
                return
            if self.tracker.complete_todo(record_id):
                self.refresh_todos()
                self.status.set('任务已标记完成')
            else:
                self.refresh_todos()
                self.status.set('该任务已不存在，列表已刷新')
        except sqlite3.Error as exc:
            self.show_message('操作失败', str(exc), parent=self)

    def delete_selected_todo(self):
        if not self.can_edit_current():
            return
        record_id = self._selected_id(self.todo_tree)
        if record_id is None or not self.show_message('确认删除', '删除选中的待办事项？\n删除后 10 秒内可在底部状态栏撤销。', parent=self, confirm=True):
            return
        try:
            snapshot = self.tracker.conn.execute(
                'SELECT id, task, status, priority, due_date, created_at, completed_at, '
                'cancel_reason, reminder_enabled, reminder_snooze_until, reminder_last_day '
                'FROM todos WHERE id=?', (record_id,)
            ).fetchone()
            deleted = self.tracker.delete_todo(record_id)
            self.refresh_todos()
            if deleted and snapshot is not None:
                self._offer_undo('todo', snapshot, f'待办 [{record_id}] 已删除')
            elif deleted:
                self.status.set('任务已删除')
            else:
                self.status.set('该任务已不存在，列表已刷新')
        except sqlite3.Error as exc:
            self.show_message('删除失败', str(exc), parent=self)

    def _start_tray(self):
        def worker():
            try:
                import pystray
                from PIL import Image, ImageDraw
                image = Image.new('RGB', (64, 64), '#2563eb')
                draw = ImageDraw.Draw(image)
                draw.rectangle((14, 12, 50, 52), fill='white')
                for y in (22, 32, 42):
                    draw.line((21, y, 43, y), fill='#2563eb', width=3)
                menu = pystray.Menu(
                    pystray.MenuItem('打开主界面', lambda icon, item: self._tray_events.put('open'), default=True),
                    pystray.MenuItem('查看待办', lambda icon, item: self._tray_events.put('todos')),
                    pystray.MenuItem('彻底退出', lambda icon, item: self._tray_events.put('exit')),
                )
                self._tray = pystray.Icon('RHWorkTracker', image, 'RH · 工作记录与到期提醒', menu)

                def ready(icon):
                    if self._closing:
                        icon.stop()
                        return
                    icon.visible = True
                    self._tray_ready.set()

                self._tray.run(setup=ready)
            except Exception as exc:
                self._tray_events.put(('error', str(exc)))
            finally:
                self._tray_ready.clear()

        self._tray_thread = threading.Thread(target=worker, daemon=True)
        self._tray_thread.start()

    def hide_to_tray(self):
        if self._grab_path():
            return
        if not self._tray_ready.is_set():
            self.show_message('托盘暂不可用', '托盘尚未就绪或启动失败，请稍后重试；仍可使用窗口最小化。', parent=self)
            return
        self.status.set('已隐藏到托盘，任务提醒继续运行')
        self.withdraw()

    def restore_window(self, todos=False):
        self.deiconify()
        self.state('normal')
        self.lift()
        if todos:
            self.todo_tab.master.select(self.todo_tab)

    def _background_tick(self):
        if self._closing:
            return
        try:
            while not self._tray_events.empty():
                event = self._tray_events.get_nowait()
                if event == 'exit':
                    self.exit_app()
                    return
                if event in ('open', 'todos'):
                    self.restore_window(event == 'todos')
                elif isinstance(event, tuple):
                    self.restore_window()
                    self.status.set(f'托盘启动失败：{event[1]}')
            # 不在编辑或确认的模态窗口期间弹出新的提醒。
            if not self._grab_path():
                # 提醒精度为分钟：同一分钟内不重复查询，仅待办变更时重新计算。
                stamp = datetime.now().strftime('%Y%m%d%H%M')
                if stamp != self._reminder_stamp or self._reminders_dirty:
                    self._reminder_stamp = stamp
                    self._reminders_dirty = False
                    reminders = self.tracker.due_reminders()
                    cycle_due = self.tracker.consume_due_reminders()
                    # 合并两类提醒，统一弹出一个卡片
                    combined = [('todo',) + r for r in reminders]
                    combined += [('cycle',) + r for r in cycle_due]
                    # 每类各自判重：同一天同一条只能弹一次（用 last_fired 标记）
                    signature = tuple(sorted(combined))
                    if self._reminder_window is not None and not self._reminder_window.winfo_exists():
                        self._reminder_window = None
                    if self._reminder_window is not None and signature != self._reminder_signature:
                        self._reminder_window.destroy()
                        self._reminder_window = None
                    if combined and self._reminder_window is None:
                        self._show_reminder(combined)
                    if cycle_due:
                        self.refresh_reminders()
        except (sqlite3.Error, tk.TclError) as exc:
            self.status.set(f'提醒检查失败：{exc}')
        finally:
            if not self._closing:
                self._background_id = self.after(1000, self._background_tick)

    def _show_reminder(self, combined):
        """统一的右下角提醒卡片。combined 格式：
          [('todo', record_id, task, due), ('cycle', record_id, title, fire_time), ...]
        两种类型一起弹，分别展示各自的内容片段。
        """
        # 兼容旧调用（纯 todo 三元组）
        if combined and isinstance(combined[0], tuple) and len(combined[0]) == 3:
            combined = [('todo',) + r for r in combined]
        window = tk.Toplevel(self)
        self._reminder_window = window
        self._reminder_signature = tuple(combined)
        has_todo = any(r[0] == 'todo' for r in combined)
        has_cycle = any(r[0] == 'cycle' for r in combined)
        parts = []
        if has_todo:
            parts.append('待办到期')
        if has_cycle:
            parts.append('循环提醒')
        window.title('RH · ' + ' / '.join(parts))
        window.geometry('420x320')
        window.resizable(False, False)
        window.attributes('-topmost', True)
        frame = ttk.Frame(window, padding=8)
        frame.pack(fill='both', expand=True)

        today = date.today()

        # 汇总条
        todo_n = sum(1 for r in combined if r[0] == 'todo')
        cycle_n = sum(1 for r in combined if r[0] == 'cycle')
        sub = []
        if todo_n:
            sub.append(f'{todo_n} 项待办')
        if cycle_n:
            sub.append(f'{cycle_n} 项循环提醒')
        ttk.Label(frame, text='  ·  '.join(sub), style='Summary.TLabel').pack(anchor='w')

        # 内容区：把所有条目列出来
        text_frame = ttk.Frame(frame)
        text_frame.pack(fill='both', expand=True, pady=4)
        text = tk.Text(text_frame, height=8, wrap='word',
                       font=('Microsoft YaHei UI', 10),
                       bg='#f8fafc', relief='flat', borderwidth=0)
        text_scroll = ttk.Scrollbar(text_frame, orient='vertical', command=text.yview)
        text.configure(yscrollcommand=text_scroll.set)
        text_scroll.pack(side='right', fill='y')
        text.tag_configure('todo_tag', foreground='#b45309',
                           font=('Microsoft YaHei UI', 10, 'bold'))
        text.tag_configure('cycle_tag', foreground='#1d4ed8',
                           font=('Microsoft YaHei UI', 10, 'bold'))
        text.tag_configure('meta_tag', foreground='#64748b',
                           font=('Microsoft YaHei UI', 9))
        first = True
        for r in combined:
            if not first:
                text.insert('end', '\n')
            first = False
            rtype = r[0]
            if rtype == 'todo':
                _, record_id, task, due = r
                deadline = self.parse_deadline(due)
                days = (deadline.date() - today).days
                if self.has_deadline_time(due):
                    delta = deadline - datetime.now()
                    count = max(1, math.ceil(abs(delta.total_seconds()) / 60))
                    timing = f'已逾期 {count} 分钟' if delta.total_seconds() < 0 else f'距截止 {count} 分钟'
                else:
                    timing = '明天到期' if days == 1 else '今天到期' if days == 0 else f'已逾期 {-days} 天'
                text.insert('end', '【待办到期】', 'todo_tag')
                text.insert('end', task.strip() + '\n')
                text.insert('end', f'   {timing}  ·  截止 {self.display_deadline(due)}', 'meta_tag')
            elif rtype == 'cycle':
                _, record_id, title, fire_time = r
                text.insert('end', '【循环提醒】', 'cycle_tag')
                text.insert('end', title.strip() + '\n')
                text.insert('end', f'   今天 {fire_time or "09:00"} 触发', 'meta_tag')
        text.configure(state='disabled')
        text.pack(side='left', fill='both', expand=True)

        feedback = tk.StringVar(window)
        ttk.Label(frame, textvariable=feedback, wraplength=400).pack(fill='x')

        def _dismiss_all():
            """关闭本次提醒（循环提醒本来就是消费式的，已在 consume_due_reminders 里推进了 next_fire）。"""
            # todos 不自动 dismiss，留给用户自己处理（避免误关闭）；cycle 已经推进了 next_fire，
            # 这里只是把窗口关掉。
            window.destroy()
            self._reminder_window = None
            self._reminder_signature = None

        buttons = ttk.Frame(frame)
        buttons.pack(fill='x', pady=(6, 0))
        buttons.columnconfigure((0,), weight=1)
        ttk.Button(buttons, text='关闭本次提醒', command=_dismiss_all).grid(
            row=0, column=0, sticky='ew', padx=2, pady=2)

        window.protocol('WM_DELETE_WINDOW', _dismiss_all)
        self.center_dialog(window, corner=True)

    def on_close(self):
        if self._tray_ready.is_set():
            self.hide_to_tray()
        else:
            self.exit_app()

    def exit_app(self):
        if self._closing:
            return
        self._closing = True
        self.after_cancel(self._background_id)
        if self._undo_job is not None:
            self.after_cancel(self._undo_job)
            self._undo_job = None
        # 只要托盘对象存在就 stop()：setup 未完成时 _tray_ready 仍是 False，
        # 但跳过 stop 会让守护线程随进程硬退出，任务栏残留幽灵图标。
        if self._tray is not None:
            self._tray.stop()
            self._tray_thread.join(timeout=1)
        if self.archive_conn is not None:
            self.archive_conn.close()
        self.tracker.close()
        self.destroy()


if __name__ == '__main__':
    set_app_user_model_id('vitesco.rh.worktracker.1')
    WorkTrackerApp().mainloop()