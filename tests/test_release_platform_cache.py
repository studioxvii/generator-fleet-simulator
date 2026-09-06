import os
import subprocess
from pathlib import Path


def test_anonymous_pulls_work_with_single_platform_digest_cache():
    workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/docker-publish.yml").read_text()
    section = workflow.split("      - name: Verify anonymous image pull\n", 1)[1]
    section = section.split("      - name: Smoke exact amd64 release digest", 1)[0]
    script = section.split("        run: |\n", 1)[1]
    script = "\n".join(line[10:] for line in script.splitlines())
    # Model the classic Docker store: the second architecture cannot overwrite
    # the first architecture while its digest reference remains cached.
    docker_stub = r'''
    pulls=0
    cached_platform=""
    docker() {
      if [ "$1" = "pull" ]; then
        if [ -n "$cached_platform" ] && [ "$cached_platform" != "$3" ]; then
          echo "cannot overwrite digest" >&2
          return 1
        fi
        cached_platform="$3"
        pulls=$((pulls + 1))
      elif [ "$1 $2" = "image rm" ]; then
        cached_platform=""
      else
        return 2
      fi
    }
    '''
    result = subprocess.run(
        ["bash", "-e", "-c", docker_stub + script + '\n[ "$pulls" = 2 ] && [ -z "$cached_platform" ]'],
        env={**os.environ, "IMAGE_REPOSITORY": "example/simulator", "IMAGE_DIGEST": "sha256:" + "a" * 64},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
