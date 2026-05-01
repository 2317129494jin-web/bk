from pathlib import Path


def pre_find_module_path(api):
    api.search_dirs = [
        str(Path(r"C:\Users\Administrator\Desktop\bidking-bot\.python311\Lib"))
    ]
