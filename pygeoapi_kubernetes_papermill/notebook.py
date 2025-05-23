# =================================================================
#
# Authors: Bernhard Mallinger <bernhard.mallinger@eox.at>
#
# Copyright (C) 2020 EOX IT Services GmbH <https://eox.at>
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation
# files (the "Software"), to deal in the Software without
# restriction, including without limitation the rights to use,
# copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the
# Software is furnished to do so, subject to the following
# conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES
# OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
# HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
# WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
# FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
# OTHER DEALINGS IN THE SOFTWARE.
#
# =================================================================

from base64 import b64encode, b64decode
from dataclasses import dataclass
from datetime import datetime, date
import functools
import json
import logging
import mimetypes
import operator
from pathlib import PurePath, Path
import os
import re
import time
from pygeoapi.util import ProcessExecutionMode
import scrapbook
import scrapbook.scraps
from typing import Iterable, Optional, Any
from typed_json_dataclass import TypedJsonMixin

from kubernetes import client as k8s_client

from .kubernetes import (
    KubernetesProcessor,
    current_namespace,
    format_annotation_key,
)
from .common import (
    ContainerKubernetesProcessorMixin,
    job_id_from_job_name,
    ExtraConfig,
    ProcessorClientError,
    drop_none_values,
    JOVIAN_UID,
    JOVIAN_GID,
    setup_byoa_results_dir_cmd,
    JobDict,
)

LOGGER = logging.getLogger(__name__)


#: Process metadata and description
PROCESS_METADATA = {
    "version": "0.1.0",
    "id": "execute-notebook",
    "title": "notebooks on kubernetes with papermill",
    "description": "",
    "keywords": ["notebook"],
    "links": [
        {
            "type": "text/html",
            "rel": "canonical",
            "title": "eurodatacube",
            "href": "https://eurodatacube.com",
            "hreflang": "en-US",
        }
    ],
    "inputs": {
        "notebook": {
            "title": "notebook file",
            "schema": {
                "type": "string",
            },
            "minOccurs": 1,
            "maxOccurs": 1,
            "metadata": None,  # TODO how to use?
            "keywords": [""],
        },
    },
    "outputs": {},
    "example": {},
    "jobControlOptions": [
        ProcessExecutionMode.async_execute.value,
        ProcessExecutionMode.sync_execute.value,
    ],
}


CONTAINER_HOME = Path("/home/jovyan")
S3_MOUNT_PATH = CONTAINER_HOME / "s3"
# we wait for the s3 mount (couldn't think of something better than this):
# * first mount point is emptyDir owned by root.
# * then it will be chowned to user by s3fs bash script, and finally also chowned
#   to group by s3fs itself.
# so when both uid and gid are set up, we should be good to go
# however the mount may fail, in which case we don't want to wait forever,
# so we count attempts
S3_MOUNT_WAIT_CMD = (
    "ATTEMPTS=0; "
    "while "
    f" [ \"$(stat -c '%u %g' '{S3_MOUNT_PATH}')\" != '{JOVIAN_UID} {JOVIAN_GID}' ] "
    " && [ $((ATTEMPTS++)) -lt 1000 ] "
    "; do echo 'wait for s3 mount'; sleep 0.05 ; done &&"
    ' echo "mount after $ATTEMPTS attempts" && '
)

# NOTE: git checkout container needs a dir for volume and a nested dir for checkout
GIT_CHECKOUT_PATH = CONTAINER_HOME / "git" / "algorithm"

# NOTE: this is not where we store result notebooks (job-output), but where the algorithms
#       should store their result data
RESULT_DATA_PATH = PurePath("/home/jovyan/result-data")


@dataclass(frozen=True)
class RequestParameters(TypedJsonMixin):
    notebook: PurePath
    kernel: Optional[str] = None
    image: Optional[str] = None
    output_dirname: Optional[str] = None
    output_filename: Optional[str] = None
    parameters: str = ""
    cpu_limit: Optional[str] = None
    mem_limit: Optional[str] = None
    cpu_requests: Optional[str] = None
    mem_requests: Optional[str] = None
    result_data_directory: Optional[str] = None
    git_revision: Optional[str] = None
    run_on_fargate: Optional[bool] = False
    node_purpose: Optional[str] = ""

    @classmethod
    def from_dict(cls, data: dict) -> "RequestParameters":
        data_preprocessed: dict[str, Any] = {
            **data,
            "notebook": PurePath(data["notebook"]),
        }
        # translate from json to base64
        if parameters_json := data_preprocessed.pop("parameters_json", None):
            data_preprocessed["parameters"] = b64encode(
                json.dumps(parameters_json).encode()
            ).decode()

        # don't use TypedJsonMixin.from_dict, return type is wrong
        return cls(**data_preprocessed)


class PapermillNotebookKubernetesProcessor(
    ContainerKubernetesProcessorMixin, KubernetesProcessor
):
    def __init__(self, processor_def: dict) -> None:
        super().__init__(processor_def, PROCESS_METADATA)

        # TODO: config file parsing (typed-json-dataclass? pydantic!)
        self.default_image: str = processor_def["default_image"]
        self.allowed_images_regex: str = processor_def["allowed_images_regex"]
        self.image_pull_secret: str = processor_def["image_pull_secret"]
        self.s3: Optional[dict[str, str]] = processor_def.get("s3")
        self.home_volume_claim_name: str = processor_def["home_volume_claim_name"]
        self.extra_pvcs: list = processor_def["extra_pvcs"]
        self.extra_volumes: list = processor_def["extra_volumes"]
        self.extra_volume_mounts: list = processor_def["extra_volume_mounts"]
        self.jupyer_base_url: str = processor_def["jupyter_base_url"]
        self.base_output_directory: Path = Path(processor_def["output_directory"])
        self.results_in_output_dir: bool = bool(
            processor_def.get("results_in_output_dir")
        )
        self.secrets = processor_def["secrets"]
        self.checkout_git_repo: Optional[dict] = processor_def.get("checkout_git_repo")
        self.log_output: bool = processor_def["log_output"]
        self.default_node_purpose: str = processor_def["default_node_purpose"]
        self.allowed_node_purposes_regex: str = processor_def[
            "allowed_node_purposes_regex"
        ]
        self.tolerations: list = processor_def["tolerations"]
        self.job_service_account: str = processor_def["job_service_account"]
        self.allow_fargate: bool = processor_def["allow_fargate"]
        self.auto_mount_secrets: bool = processor_def["auto_mount_secrets"]
        self.node_purpose_label_key: str = processor_def["node_purpose_label_key"]
        self.run_as_user: Optional[int] = processor_def["run_as_user"]
        self.run_as_group: Optional[int] = processor_def["run_as_group"]
        self.conda_store_groups: list[str] = processor_def["conda_store_groups"]
        self.extra_resource_limits: dict[str, str] = processor_def[
            "extra_resource_limits"
        ]
        self.extra_resource_requests: dict[str, str] = processor_def[
            "extra_resource_requests"
        ]

        if self.s3:
            # NOTE: for notebooks, we currently only support this mount path
            #       otherwise wait command would need to be changed
            self.s3["mount_path"] = str(S3_MOUNT_PATH)

    def create_job_pod_spec(
        self,
        data: dict,
        job_name: str,
    ) -> KubernetesProcessor.JobPodSpec:
        LOGGER.debug("Starting job with data %s", data)

        try:
            requested = RequestParameters.from_dict(data)
        except (TypeError, KeyError) as e:
            raise ProcessorClientError(user_msg=f"Invalid parameter: {e}") from e

        image = self._image(requested.image)

        output_notebook = self.setup_output(requested, job_id_from_job_name(job_name))

        extra_podspec = self._extra_podspec(requested)
        extra_podspec["tolerations"] += [
            k8s_client.V1Toleration(
                # alwyas tolerate gpu, is selected by node group only
                key="nvidia.com/gpu",
                operator="Exists",
                effect="NoSchedule",
            ),
            k8s_client.V1Toleration(
                key="hub.jupyter.org/dedicated",
                operator="Exists",
                effect="NoSchedule",
            ),
        ]

        if self.image_pull_secret:
            extra_podspec["image_pull_secrets"] = [
                k8s_client.V1LocalObjectReference(name=self.image_pull_secret)
            ]

        extra_config = self._extra_configs(git_revision=requested.git_revision)

        papermill_slack_cmd = (
            'if [ -n "$PAPERMILL_SLACK_WEBHOOK_URL" ] ; '
            f'then papermill_slack "{output_notebook}"; fi '
        )

        papermill_cmd = (
            f"papermill "
            f'"{requested.notebook}" '
            f'"{output_notebook}" '
            "--engine kubernetes_job_progress "
            "--request-save-on-cell-execute "
            "--autosave-cell-every 60 "
            f'--cwd "{working_dir(requested.notebook)}" '
            + ("--log-output " if self.log_output else "")
            + (f"-k {requested.kernel} " if requested.kernel else "")
            + (f'-b "{requested.parameters}" ' if requested.parameters else "")
        )

        notebook_container = k8s_client.V1Container(
            name="notebook",
            image=image,
            command=[
                "bash",
                # NOTE: we pretend that the shell is interactive such that it
                #       sources /etc/bash.bashrc which activates the default conda
                #       env. This is ok because the default interactive shell must
                #       have PATH set up to include papermill since regular user
                #       should also be able to execute default env commands without extra
                #       setup
                "-i",
                "-c",
                (
                    setup_conda_store_group_cmd(self.conda_store_groups)
                    if self.conda_store_groups
                    else ""
                )
                + (S3_MOUNT_WAIT_CMD if self.s3 else "")
                + (
                    setup_byoa_results_dir_cmd(
                        parent_of_subdir=CONTAINER_HOME,
                        subdir=requested.result_data_directory,
                        result_data_path=RESULT_DATA_PATH,
                        job_name=job_name,
                    )
                    if requested.result_data_directory
                    else ""
                )
                +
                # TODO: weird bug: removing this ls results in a PermissionError when
                #       papermill later writes to the file. This only happens sometimes,
                #       but when it occurs, it does so consistently. I'm leaving that in
                #       for now since that command doesn't do any harm.
                #       (it will be a problem if there are ever a lot of output files,
                #       especially on s3fs)
                f"ls -la {output_notebook.parent} >/dev/null"
                " && "
                + papermill_cmd
                + "; PAPERMILL_EXIT_CODE=$? "
                + " && "
                + papermill_slack_cmd
                + " && "
                + "exit $PAPERMILL_EXIT_CODE",
            ],
            working_dir=str(CONTAINER_HOME),
            volume_mounts=extra_config.volume_mounts,
            resources=self._resource_requirements(requested),
            env=[
                # this is provided in jupyter worker containers and we also use it
                # for compatibility checks
                k8s_client.V1EnvVar(name="JUPYTER_IMAGE", value=image),
                k8s_client.V1EnvVar(name="JOB_NAME", value=job_name),
                k8s_client.V1EnvVar(
                    name="PROGRESS_ANNOTATION", value=format_annotation_key("progress")
                ),
                k8s_client.V1EnvVar(name="HOME", value=str(CONTAINER_HOME)),
            ]
            + (
                [
                    k8s_client.V1EnvVar(
                        name="RESULTS_DIRECTORY", value=str(output_notebook.parent)
                    ),
                ]
                if self.results_in_output_dir
                else []
            ),
            env_from=extra_config.env_from,
        )
        extra_annotations = {
            "result-notebook": str(output_notebook),
            "executed-notebook": str(requested.notebook),
            # save parameters but make sure the string is not too long
            "parameters": json.dumps(data)[:8000],
        }

        return KubernetesProcessor.JobPodSpec(
            pod_spec=k8s_client.V1PodSpec(
                restart_policy="Never",
                # NOTE: first container is used for status check
                containers=[notebook_container] + extra_config.containers,
                init_containers=extra_config.init_containers,
                volumes=extra_config.volumes,
                # we need this to be able to terminate the sidecar container
                # https://github.com/kubernetes/kubernetes/issues/25908
                share_process_namespace=True,
                service_account=self.job_service_account,
                security_context=k8s_client.V1PodSecurityContext(
                    **({"run_as_user": self.run_as_user} if self.run_as_user else {}),
                    **(
                        {"run_as_group": self.run_as_group} if self.run_as_group else {}
                    ),
                ),
                **extra_podspec,
                enable_service_links=False,
            ),
            extra_annotations=extra_annotations,
            extra_labels={"runtime": "fargate"} if requested.run_on_fargate else {},
        )

    def _extra_configs(  # type: ignore[override]
        self,
        git_revision: Optional[str],
    ) -> ExtraConfig:
        def extra_configs() -> Iterable[ExtraConfig]:
            if self.home_volume_claim_name:
                yield home_volume_config(self.home_volume_claim_name)

            # DEPRECATED
            yield from (
                extra_pvc_config(extra_pvc={**extra_pvc, "num": num})
                for num, extra_pvc in enumerate(self.extra_pvcs)
            )

            if self.auto_mount_secrets:
                yield extra_auto_secrets()

            if self.checkout_git_repo:
                yield git_checkout_config(
                    git_revision=git_revision,
                    **self.checkout_git_repo,
                )

            if self.conda_store_groups:
                yield conda_store_group_volume_mounts(self.conda_store_groups)

        return super()._extra_configs() + functools.reduce(
            operator.add, extra_configs(), ExtraConfig()
        )

    def _image(self, requested_image: Optional[str]) -> str:
        if requested_image:
            if not self.allowed_images_regex:
                raise ProcessorClientError(user_msg="Custom images are not allowed")
            elif not re.fullmatch(self.allowed_images_regex, requested_image):
                raise ProcessorClientError(
                    user_msg=f"Image {requested_image} is not allowed, "
                    f"only {self.allowed_images_regex}"
                )
            else:
                return requested_image
        else:
            return self.default_image

    def _resource_requirements(self, requested: RequestParameters):
        return k8s_client.V1ResourceRequirements(
            limits=drop_none_values(
                {
                    "cpu": requested.cpu_limit,
                    "memory": requested.mem_limit,
                }
                | self.extra_resource_limits
            ),
            requests=drop_none_values(
                {
                    "cpu": requested.cpu_requests,
                    "memory": requested.mem_requests,
                }
                | self.extra_resource_requests
            ),
        )

    def setup_output(self, requested: RequestParameters, job_id: str) -> Path:
        output_dirname_validated = (
            PurePath(requested.output_dirname).name
            if requested.output_dirname
            else None
        )
        output_directory = (
            self.base_output_directory
            / date.today().isoformat()
            / (output_dirname_validated if output_dirname_validated else ".")
        )

        if self.results_in_output_dir:
            results_dir = now_formatted() + "-" + requested.notebook.stem
            output_notebook_filename_in_results = (
                requested.notebook.stem + "_result" + requested.notebook.suffix
            )
            output_notebook = (
                output_directory / results_dir / output_notebook_filename_in_results
            )
        else:
            output_filename_validated = PurePath(
                requested.output_filename
                if requested.output_filename
                else (
                    requested.notebook.stem
                    + f"_result_{now_formatted()}_{job_id}.ipynb"
                )
            ).name
            output_notebook = output_directory / output_filename_validated
        # create output directory owned by root (readable, but not changeable by user)
        output_notebook.parent.mkdir(exist_ok=True, parents=True)

        output_notebook.touch(exist_ok=False)
        # TODO: reasonable error when output notebook already exists
        os.chown(output_notebook, uid=JOVIAN_UID, gid=JOVIAN_GID)
        os.chmod(output_notebook, mode=0o664)

        return output_notebook

    def __repr__(self):
        return "<PapermillNotebookKubernetesProcessor> {}".format(self.name)


def notebook_job_output(result: JobDict) -> tuple[Optional[str], Any]:
    # NOTE: this assumes that we have user home under the same path as jupyter
    notebook_path = Path(result["result-notebook"])

    _wait_for_result_file(notebook_path)
    scraps = scrapbook.read_notebook(str(notebook_path)).scraps

    LOGGER.debug("Retrieved scraps from notebook: %s", scraps)

    if not scraps:
        return (None, {})
    elif result_file_scrap := scraps.get("result-file"):
        # if available, prefer file output
        specified_path = Path(result_file_scrap.data)
        result_file_path = (
            specified_path
            if specified_path.is_absolute()
            else CONTAINER_HOME / specified_path
        )
        # NOTE: use python-magic or something more advanced if necessary
        mime_type = mimetypes.guess_type(result_file_path)[0]
        return (mime_type, result_file_path.read_bytes())
    elif len(scraps) == 1:
        # if there's only one item, return it right away with correct content type.
        # this way, you can show e.g. an image in the browser.
        # otherwise, we just return the scrap structure
        return serialize_single_scrap(next(iter(scraps.values())))
    else:
        # TODO: support serializing multiple scraps, possibly according to result schema:
        # https://github.com/opengeospatial/wps-rest-binding/blob/master/core/openapi/schemas/result.yaml
        return serialize_single_scrap(next(iter(scraps.values())))


def _wait_for_result_file(notebook_path: Path) -> None:
    # If the result file is queried immediately after the job has finished, it's likely
    # that the s3fs mount has not yet received the changes that were written by the job.
    # So instead of failing right away here, we detect the situation and wait.
    for _ in range(20):
        # NOTE: s3fs will only be refreshed on operations such as these. However
        #       the refresh takes some time, which is ok here because we will catch it
        #       in a subsequent loop run
        notebook_path.open().close()
        if notebook_path.stat().st_size != 0:
            LOGGER.info("Result file present")
            break
        else:
            LOGGER.info("Waiting for result file")
            time.sleep(1)


def serialize_single_scrap(scrap: scrapbook.scraps.Scrap) -> tuple[Optional[str], Any]:
    text_mime = "text/plain"

    if scrap.display:
        # we're only interested in display_data
        # https://ipython.org/ipython-doc/dev/notebook/nbformat.html#display-data

        if scrap.display["output_type"] == "display_data":
            # data contains representations with different mime types as keys. we
            # want to prefer non-text
            mime_type = next(
                (f for f in scrap.display["data"] if f != text_mime),
                text_mime,
            )
            item = scrap.display["data"][mime_type]
            encoded_output = item if mime_type == text_mime else b64decode(item)
            return (mime_type, encoded_output)
        else:
            return None, scrap.display
    else:
        return None, scrap.data


def now_formatted() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S-%f")


def default_output_path(notebook_path: str, job_id: str) -> str:
    filepath_without_postfix = re.sub(".ipynb$", "", notebook_path)
    return filepath_without_postfix + f"_result_{now_formatted()}_{job_id}.ipynb"


def working_dir(notebook_path: PurePath) -> PurePath:
    abs_notebook_path = (
        notebook_path
        if notebook_path.is_absolute()
        else (CONTAINER_HOME / notebook_path)
    )
    return abs_notebook_path.parent


def home_volume_config(home_volume_claim_name: str) -> ExtraConfig:
    return ExtraConfig(
        volumes=[
            k8s_client.V1Volume(
                persistent_volume_claim=k8s_client.V1PersistentVolumeClaimVolumeSource(
                    claim_name=home_volume_claim_name,
                ),
                name="home",
            )
        ],
        volume_mounts=[
            k8s_client.V1VolumeMount(
                mount_path=str(CONTAINER_HOME),
                name="home",
            )
        ],
    )


def extra_pvc_config(extra_pvc: dict) -> ExtraConfig:
    # DEPRECATED
    extra_name = f"extra-{extra_pvc['num']}"
    return ExtraConfig(
        volumes=[
            k8s_client.V1Volume(
                persistent_volume_claim=k8s_client.V1PersistentVolumeClaimVolumeSource(
                    claim_name=extra_pvc["claim_name"],
                ),
                name=extra_name,
            )
        ],
        volume_mounts=[
            k8s_client.V1VolumeMount(
                mount_path=extra_pvc["mount_path"],
                name=extra_name,
                sub_path=extra_pvc.get("sub_path"),
            )
        ],
    )


def extra_auto_secrets() -> ExtraConfig:
    secrets: k8s_client.V1SecretList = k8s_client.CoreV1Api().list_namespaced_secret(
        namespace=current_namespace()
    )
    # yield eurodatacube and edc-my-credentials secrets, just as jupyterlab
    edc_regex = r"eurodatacube-.*default"
    edc_my_credentials_label = ("owner", "edc-my-credentials")

    return ExtraConfig(
        env_from=[
            k8s_client.V1EnvFromSource(
                secret_ref=k8s_client.V1SecretEnvSource(name=secret.metadata.name)
            )
            for secret in secrets.items
            if re.fullmatch(edc_regex, secret.metadata.name)
            or (secret.metadata.labels or {}).get(edc_my_credentials_label[0])
            == edc_my_credentials_label[1]
        ]
    )


def git_checkout_config(
    url: str, secret_name: str, git_revision: Optional[str]
) -> ExtraConfig:
    # compat for old python
    def removeprefix(self, prefix: str) -> str:
        if self.startswith(prefix):
            return self[len(prefix) :]  # noqa: E203
        else:
            return self[:]

    git_sync_mount_name = "git-sync-mount"
    git_sync_mount_path = "/tmp/git"
    git_sync_target_path = f"{git_sync_mount_path}/{GIT_CHECKOUT_PATH.name}"

    init_container = k8s_client.V1Container(
        name="git",
        image="alpine/git:v2.30.2",
        volume_mounts=[
            k8s_client.V1VolumeMount(
                name=git_sync_mount_name,
                mount_path=git_sync_mount_path,
            ),
        ],
        env=[
            k8s_client.V1EnvVar(
                name="GIT_USERNAME",
                value_from=k8s_client.V1EnvVarSource(
                    secret_key_ref=k8s_client.V1SecretKeySelector(
                        name=secret_name,
                        key="username",
                    ),
                ),
            ),
            k8s_client.V1EnvVar(
                name="GIT_PASSWORD",
                value_from=k8s_client.V1EnvVarSource(
                    secret_key_ref=k8s_client.V1SecretKeySelector(
                        name=secret_name,
                        key="password",
                    ),
                ),
            ),
        ]
        + (
            [k8s_client.V1EnvVar(name="GIT_SYNC_REV", value=git_revision)]
            if git_revision
            else []
        ),
        command=[
            "sh",
            "-c",
            "git clone "
            f"https://${{GIT_USERNAME}}:${{GIT_PASSWORD}}@{removeprefix(url, 'https://')}"
            f' "{git_sync_target_path}" '
            + (
                ""
                if not git_revision
                else f' && cd "{git_sync_target_path}" && git checkout ' + git_revision
            ),
        ],
        security_context=k8s_client.V1SecurityContext(
            run_as_user=JOVIAN_UID,
            run_as_group=JOVIAN_GID,
        ),
    )

    return ExtraConfig(
        init_containers=[init_container],
        volume_mounts=[
            k8s_client.V1VolumeMount(
                mount_path=str(GIT_CHECKOUT_PATH.parent),
                name=git_sync_mount_name,
            )
        ],
        volumes=[
            k8s_client.V1Volume(
                name=git_sync_mount_name,
                empty_dir=k8s_client.V1EmptyDirVolumeSource(),
            )
        ],
    )


def conda_store_group_volume_mounts(conda_store_groups: list[str]) -> ExtraConfig:
    return ExtraConfig(
        volumes=[
            k8s_client.V1Volume(
                persistent_volume_claim=k8s_client.V1PersistentVolumeClaimVolumeSource(
                    claim_name="conda-store-core-share",
                ),
                name="conda-store",
            )
        ],
        volume_mounts=[
            k8s_client.V1VolumeMount(
                mount_path=f"/home/conda/{group}",
                name="conda-store",
                read_only=True,
                sub_path=group,
            )
            for group in conda_store_groups
        ],
    )


def setup_conda_store_group_cmd(conda_store_groups: list[str]) -> str:
    """nb_conda_kernels setup for papermill:
    https://github.com/Anaconda-Platform/nb_conda_kernels#use-with-nbconvert-voila-papermill
    """
    env_dirs = [f"/home/conda/{group}/envs" for group in conda_store_groups]
    commands = (
        f'echo "{{envs_dirs: [{", ".join(env_dirs)}]}}" > {CONTAINER_HOME}/.condarc',
        f"mkdir -p {CONTAINER_HOME}/.jupyter",
        'echo \'{"CondaKernelSpecManager": {"kernelspec_path": "--user"}}\' > '
        f"{CONTAINER_HOME}/.jupyter/jupyter_config.json",
        "python3 -m nb_conda_kernels list",
    )
    return "".join(f"{cmd} && " for cmd in commands)
