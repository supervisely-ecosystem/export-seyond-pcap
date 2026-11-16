"""Export Seyond lidar pcap: headless task from "Download as" of a point cloud (episodes) project or dataset."""

import os

import supervisely as sly
from dotenv import load_dotenv

import workflow as w
from export import plan_export, write_archive
from settings import Settings

RESULT_DIR_NAME = "Export Seyond lidar pcap"


def upload_archive(api: sly.Api, team_id: int, task_id, local_archive: str, archive_name: str):
    """Upload the archive to Team Files and, in a task, set it as the task output."""
    remote_archive = os.path.join(sly.team_files.RECOMMENDED_EXPORT_PATH, RESULT_DIR_NAME, archive_name)
    remote_archive = api.file.get_free_name(team_id, remote_archive)
    progress = sly.tqdm_sly(
        desc=f"Upload {archive_name!r}", total=os.path.getsize(local_archive), unit="B", unit_scale=True
    )
    file_info = api.file.upload(team_id, local_archive, remote_archive, progress_cb=progress)
    sly.logger.info(f"Uploaded to Team Files: {file_info.path!r}")
    if task_id is not None:
        api.task.set_output_archive(task_id, file_info.id, archive_name, file_url=file_info.storage_path)
    return file_info


def main() -> None:
    if not sly.is_production():
        load_dotenv("local.env")
        load_dotenv(os.path.expanduser("~/supervisely.env"))

    settings = Settings.from_env()
    team_id = sly.env.team_id()
    task_id = sly.env.task_id(raise_not_found=False)
    project_id = sly.env.project_id(raise_not_found=False)
    dataset_id = sly.env.dataset_id(raise_not_found=False)
    api = sly.Api.from_env()

    dataset = None
    if dataset_id is not None:
        dataset = api.dataset.get_info_by_id(dataset_id)
        if dataset is None:
            raise ValueError(f"Dataset {dataset_id} is not found or is not accessible")
        project_id = dataset.project_id
        w.workflow_input(api, dataset_id, "dataset")
    elif project_id is not None:
        w.workflow_input(api, project_id, "project")
    else:
        raise ValueError("Run the app from a project or a dataset")
    sly.logger.info(
        f"Team: {team_id}, project: {project_id}, dataset: {dataset_id}, settings: {settings}"
    )

    plan = plan_export(api, project_id, dataset_id)
    if not plan.captures:
        raise ValueError("Nothing to export: no Seyond pcap frames in the selected project or dataset")

    work_dir = sly.app.get_data_dir()
    prefix = f"{task_id}_" if task_id is not None else ""
    suffix = f"_{dataset.name}" if dataset is not None else ""
    archive_name = f"{prefix}{plan.project.id}_{plan.project.name}{suffix}.tar"
    local_archive = os.path.join(work_dir, archive_name)
    write_archive(api, plan, local_archive, work_dir, links_only=settings.links_only)

    file_info = upload_archive(api, team_id, task_id, local_archive, archive_name)
    sly.fs.silent_remove(local_archive)
    w.workflow_output(api, file_info)


if __name__ == "__main__":
    sly.main_wrapper("main", main)
