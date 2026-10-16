# OpenShift deployment walkthrough

Run from `evaluation-dashboard/` after confirming the active `oc` context is the intended cluster. The files create resources only in `evalhub-dashboard` and read-only Role/RoleBinding objects in the source workspace `evalhub-lab`.

## 1. Create the namespace and cookie secret

```sh
oc apply -f deploy/namespace.yaml
umask 077
openssl rand -base64 32 > /tmp/model-evaluation-dashboard-cookie
oc -n evalhub-dashboard create secret generic model-evaluation-dashboard-oauth \
  --from-file=cookie-secret=/tmp/model-evaluation-dashboard-cookie \
  --dry-run=client -o yaml | oc apply -f -
rm -f /tmp/model-evaluation-dashboard-cookie
```

The OAuth cookie secret is generated at deployment time and is not stored in this repository.

## 2. Grant source read access

```sh
oc apply -f deploy/source-access.yaml
```

The dashboard service account receives `get`/`list` on EvalHub evaluations, `get` on the named EvalHub instance, and the `get`/`create` verbs required by the named `evalhubs/proxy` subresource. The backend uses that proxy only for fixed `GET` requests. It also receives the existing MLflow read-only `mlflow-operator-mlflow-view` ClusterRole through a RoleBinding scoped to `evalhub-lab`. It does not receive create/update/delete on evaluations, MLflow edit, or cluster-wide roles. Confirm the MLflow ClusterRole exists before continuing:

```sh
oc get clusterrole mlflow-operator-mlflow-view
```

## 3. Apply the app and build the image

```sh
oc apply -f deploy/app.yaml
oc -n evalhub-dashboard start-build model-evaluation-dashboard --from-dir=. --follow
oc -n evalhub-dashboard rollout status deployment/model-evaluation-dashboard
```

The `app.yaml` uses the cluster-matching OpenShift OAuth proxy image, a project-level service account, a mounted service CA for internal MLflow TLS, and the verified in-cluster MLflow service URL. The route terminates external TLS at the OpenShift router and redirects cleartext requests to HTTPS. The app API binds to pod loopback and is only reachable from the OAuth proxy sidecar.

## 4. Verify

```sh
oc -n evalhub-dashboard get pods,service,route
oc -n evalhub-dashboard get route model-evaluation-dashboard -o jsonpath='{.spec.host}{"\n"}'
oc -n evalhub-dashboard logs deployment/model-evaluation-dashboard -c app
```

Open the route in a browser and sign in through OpenShift. The app's source panel must report MLflow and EvalHub status. A healthy pod alone is not proof that source authorization works. Confirm read-only service-account permissions:

```sh
oc auth can-i get evalhubs/evalhub -n evalhub-lab --as=system:serviceaccount:evalhub-dashboard:metrics-dashboard
oc auth can-i create evalhubs/evalhub --subresource=proxy -n evalhub-lab --as=system:serviceaccount:evalhub-dashboard:metrics-dashboard
oc -n evalhub-lab get role metrics-dashboard-evalhub-reader -o yaml
oc get clusterrole mlflow-operator-mlflow-view -o yaml
```

The first two checks should return `yes`. EvalHub's `evaluations` and MLflow's `experiments` permissions are virtual API resources on this cluster, so `oc auth can-i` does not discover them. Confirm the EvalHub Role grants only `get`/`list` on `evaluations` and the MLflow ClusterRole contains no write verbs. Then verify that both live sources report `ok` in the dashboard.
