import json
import os
import subprocess
import urllib.parse

import requests
import argparse
from datetime import datetime, timedelta

API_ENDPOINT = "https://api.github.com"
PER_PAGE = 30  # max 100 defaults 30
DOCKER_ENDPOINT = "ghcr.io/"


def get_url(path):
    if path.startswith(API_ENDPOINT):
        return path
    return f"{API_ENDPOINT}{path}"


def get_base_headers():
    return {
        "Authorization": "Bearer {}".format(args.token),
        "Accept": "application/vnd.github+json",
    }


def del_req(path):
    print(f'DEL {get_url(path)}')
    res = requests.delete(get_url(path), headers=get_base_headers())
    if res.ok:
        print(f"Deleted {path}")
    else:
        print(res.text)
    return res


def get_req(path, params=None):
    if params is None:
        params = {}
    params.update(page=1)
    if "per_page" not in params:
        params["per_page"] = PER_PAGE
    url = get_url(path)
    result = []
    while True:
        print(f'GET {url} {params}')
        response = requests.get(url, headers=get_base_headers(), params=params)
        if not response.ok:
            raise Exception(response.text)
        result.extend(response.json())

        if "next" not in response.links:
            break
        url = response.links["next"]["url"]
        if "page" in params:
            del params["page"]
    return result


def get_list_packages(owner, repo_name, owner_type, package_names):
    pkgs = []
    if package_names:
        for package_name in package_names:
            clean_package_name = urllib.parse.quote(package_name, safe='')
            url = get_url(
                f"/{owner_type}s/{owner}/packages/container/{clean_package_name}"
            )
            response = requests.get(url, headers=get_base_headers())
            if not response.ok:
                if response.status_code == 404:
                    print(f'WARNING: Package {package_name} does not exist.')
                    continue
                raise Exception(response.text)
            pkgs.append(response.json())
    else:
        pkgs = get_req(
            f"/{owner_type}s/{owner}/packages",
            params={'package_type': 'container'},
        )

    # this is a strange bug in github api, it returns deleted packages
    # I open a ticket for that
    pkgs = [pkg for pkg in pkgs if not pkg["name"].startswith('deleted_')]
    if repo_name:
        pkgs = [
            pkg for pkg in pkgs if pkg.get("repository")
            and pkg["repository"]["name"].lower() == repo_name
        ]
    return pkgs


def get_all_package_versions(owner, repo_name, package_names, owner_type):
    packages = get_list_packages(
        owner=owner,
        repo_name=repo_name,
        package_names=package_names,
        owner_type=owner_type,
    )
    return {
        pkg['name']: get_all_package_versions_per_pkg(pkg["url"])
        for pkg in packages
    }


def get_all_package_versions_per_pkg(package_url):
    url = f"{package_url}/versions"
    return get_req(url)


def get_deps_pkgs(owner, pkgs):
    ids = []
    for pkg in pkgs:
        for pkg_ver in pkgs[pkg]:
            image = f"{DOCKER_ENDPOINT}{owner}/{pkg}@{pkg_ver['name']}"
            ids.extend(get_image_deps(image))
    return ids


def get_image_deps(image):
    manifest_txt = get_manifest(image)
    data = json.loads(manifest_txt)
    return [manifest['digest'] for manifest in data.get("manifests", [])]


def get_manifest(image):
    cmd = f"docker manifest inspect {image}"
    res = subprocess.run(cmd, shell=True, capture_output=True)
    if res.returncode != 0:
        print(cmd)
        raise Exception(res.stderr)
    return res.stdout.decode("utf-8")


def delete_pkgs(owner, repo_name, owner_type, package_names, untagged_only,
                except_untagged_multiplatform, older):
    if untagged_only or older > 0:
        all_packages = get_all_package_versions(
            owner=owner,
            repo_name=repo_name,
            package_names=package_names,
            owner_type=owner_type,
        )

        if except_untagged_multiplatform:
            tagged_pkgs = {
                pkg: [
                    pkg_ver for pkg_ver in all_packages[pkg]
                    if pkg_ver["metadata"]["container"]["tags"]
                ]
                for pkg in all_packages
            }
            deps_pkgs = get_deps_pkgs(owner, tagged_pkgs)
        else:
            deps_pkgs = []

        packages = [
            pkg_ver for pkg in all_packages for pkg_ver in all_packages[pkg]
            if pkg_ver["name"] not in deps_pkgs
        ]

        if untagged_only:
            packages = [
                pkg for pkg in packages
                if not pkg["metadata"]["container"]["tags"]
            ]

        if older > 0:
            # comparing dates as strings works for this format
            # and is faster than parsing strings to dates before comparing.
            timestamp = (datetime.now() - timedelta(seconds=older)).strftime('%Y-%m-%dT%H:%M:%SZ')
            packages = [
                pkg for pkg in packages
                if pkg['updated_at'] < timestamp
            ]
    else:
        packages = get_list_packages(
            owner=owner,
            repo_name=repo_name,
            package_names=package_names,
            owner_type=owner_type,
        )
    status = [del_req(pkg["url"]).ok for pkg in packages]
    len_ok = len([ok for ok in status if ok])
    len_fail = len(status) - len_ok

    print(f"Deleted {len_ok} package")
    if "GITHUB_OUTPUT" in os.environ:
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write(f"num_deleted={len_ok}\n")
    if len_fail > 0:
        raise Exception(f"fail delete {len_fail}")


def str2bool(v):
    if isinstance(v, bool):
        return v
    if v.lower() in ("yes", "true", "t", "y", "1"):
        return True
    elif v.lower() in ("no", "false", "f", "n", "0"):
        return False
    else:
        raise argparse.ArgumentTypeError("Boolean value expected.")


def get_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--token",
        type=str,
        required=True,
        help="Github Personal access token with delete:packages permissions",
    )
    parser.add_argument("--repository_owner",
                        type=str,
                        required=True,
                        help="The repository owner name")
    parser.add_argument(
        "--repository",
        type=str,
        required=False,
        default="",
        help="Delete only repository name",
    )
    parser.add_argument(
        "--package_names",
        type=str,
        required=False,
        default="",
        help="Delete only comma separated package names",
    )
    parser.add_argument(
        "--untagged_only",
        type=str2bool,
        help="Delete only package versions without tag",
    )
    parser.add_argument(
        "--owner_type",
        choices=["org", "user"],
        default="org",
        help="Owner type (org or user)",
    )
    parser.add_argument(
        "--except_untagged_multiplatform",
        type=str2bool,
        help=
        "Except untagged multiplatform packages from deletion (only for --untagged_only) needs docker installed",
    )
    parser.add_argument(
        "--older",
        type=int,
        default=0,
        help="Older than time in seconds",
    )
    args = parser.parse_args()
    if "/" in args.repository:
        repository_owner, repository = args.repository.split("/")
        if repository_owner != args.repository_owner:
            raise Exception(
                f"Mismatch in repository:{args.repository} and repository_owner:{args.repository_owner}"
            )
        args.repository = repository
    args.repository = args.repository.lower()
    args.repository_owner = args.repository_owner.lower()
    args.package_names = args.package_names.lower()
    args.package_names = [p.strip() for p in args.package_names.split(",")
                          ] if args.package_names else []
    return args


if __name__ == "__main__":
    args = get_args()
    delete_pkgs(
        owner=args.repository_owner,
        repo_name=args.repository,
        package_names=args.package_names,
        untagged_only=args.untagged_only,
        owner_type=args.owner_type,
        except_untagged_multiplatform=args.except_untagged_multiplatform,
        older=args.older)
