## Training Controller Service

## Run on k8s cluster
Pre-requisites:
* Must have at least one GPU node (preferably K80 GPU or higher) and at least two CPUs as part of the cluster with at least 10 GiB memory as well.
* Make sure appropriate rbac is set up.

```
* To setup Minio
helm install --set accessKey=myaccesskey,secretKey=mysecretkey minio minio/minio
* To setup rbac
kubectl apply -f rbac.yaml
* To install NVIDIA gpu driver
kubectl apply -f https://raw.githubusercontent.com/NVIDIA/k8s-device-plugin/v0.6.0/nvidia-device-plugin.yml
* To deploy training-controller service
kubectl apply -f training_controller.yaml
```
---
---
#### Methodology
* Training controller service is subscribed to the Nats subject called "train"
* When it receives any content from this subject, it will launch the necessary steps.
* Controller will first fetch the logs from Elasticsearch that will be used by the NuLog training job.
* Nulog model is then trained through a job.
* Once, Nulog model training has been completed, it will send a message to the Nats subject indicating that a new model is ready to be used.
*
Payload sent to the "train" Nats subject should be in this format
```
    payload = {"model_to_train": "nulog","time_intervals": [{"start_ts": 1617039360000000000, "end_ts": 1617039450000000000}, {"start_ts": 1617039510000000000, "end_ts": 1617039660000000000}]}

```

Use nats-box to send training signal manually:
```
kubectl run -i --rm --tty nats-box --image=synadia/nats-box --restart=Never
nats-pub -s nats://nats_client:VfU6TcAl9x@nats-client.default.svc:4222 train '{"model_to_train": "nulog","time_intervals": [{"start_ts": 1619661600000000000, "end_ts": 1619671569000000000}]}'
```
* You can then view the pods and jobs of your cluster to verify that the Nulog model is undergoing training.

## Contributing
We use `pre-commit` for formatting auto-linting and checking import. Please refer to [installation](https://pre-commit.com/#installation) to install the pre-commit or run `pip install pre-commit`. Then you can activate it for this repo. Once it's activated, it will lint and format the code when you make a git commit. It makes changes in place. If the code is modified during the reformatting, it needs to be staged manually.

```
# Install
pip install pre-commit

# Install the git commit hook to invoke automatically every time you do "git commit"
pre-commit install

# (Optional)Manually run against all files
pre-commit run --all-files
```
