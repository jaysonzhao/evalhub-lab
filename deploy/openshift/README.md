# EvalHub lab deployment

This directory deploys an isolated EvalHub namespace and PostgreSQL database. It does not change shared OpenShift AI components or services.

Create the `evalhub-lab` namespace first, then create two namespace-local Secrets before applying the database and EvalHub manifests:

- `evalhub-postgres-credentials`: `POSTGRESQL_DATABASE`, `POSTGRESQL_USER`, and `POSTGRESQL_PASSWORD` for the Red Hat PostgreSQL image.
- `evalhub-db-credentials`: a single `db-url` key containing `postgresql://USER:PASSWORD@evalhub-postgres.evalhub-lab.svc:5432/evalhub?sslmode=disable` with URL-encoded credentials.

Keep the generated secret material outside Git. Apply `namespace.yaml`, create the two Secrets, apply `postgres.yaml`, wait for the database to become ready, and then apply `evalhub.yaml`. The EvalHub operator creates the application deployment and Route. Use the Route returned by `oc get route -n evalhub-lab` as `EVALHUB_URL`.

The sample model endpoint is the cluster MaaS OpenAI-compatible base URL. It needs a MaaS API key for inference, and the model must be healthy before an evaluation can run.
