# Repository operating notes

## NUC production deployment

- Update the NUC checkout directly from the repository with:

  ```sh
  git pull --ff-only
  ```

- Do not configure or override an SSH identity for this pull. Do not transfer a
  Git bundle as a workaround; the NUC repository is intended to pull normally.
- Always bring up the production ROMmates container with the NUC-local
  `/home/ryanho87/rommates/docker-compose.yaml` file:

  ```sh
  docker compose -f docker-compose.yaml up -d --build
  ```

- Do not use the tracked `compose.yaml` for the NUC deployment. It is a reference
  configuration from GitHub and does not contain the production routing, mounts,
  and APNs setup.
- Preserve the NUC-local `docker-compose.yaml`; it is intentionally not tracked by Git.
