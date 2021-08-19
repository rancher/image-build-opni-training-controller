# Standard Library
import logging
import os
import shutil
import subprocess

# Third Party
import pandas as pd
from elasticsearch import Elasticsearch
from elasticsearch.helpers import scan

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(message)s")
ES_ENDPOINT = os.environ["ES_ENDPOINT"]
ES_USERNAME = os.environ["ES_USERNAME"]
ES_PASSWORD = os.environ["ES_PASSWORD"]
FORMATTED_ES_ENDPOINT = (
    f"https://{ES_USERNAME}:{ES_PASSWORD}@" + ES_ENDPOINT.split("//")[-1]
)


class PrepareTrainingLogs:
    def __init__(self):
        self.WORKING_DIR = os.getenv("TRAINING_DATA_PATH", "/var/opni-data")
        self.TRAINING_DIR = os.path.join(self.WORKING_DIR, "windows")
        self.ES_DUMP_DIR = os.path.join(self.WORKING_DIR, "esdump_path")
        self.ES_DUMP_SAMPLE_LOGS_PATH = os.path.join(
            self.WORKING_DIR, "sample_logs.json"
        )

    def disk_size(self):
        # Fetch size of disk
        logging.info("Fetching size of the disk")
        total, used, free = shutil.disk_usage("/")
        logging.info("Disk Total: %d GiB" % (total // (2 ** 30)))
        logging.info("Disk Used: %d GiB" % (used // (2 ** 30)))
        logging.info("Disk Free: %d GiB" % (free // (2 ** 30)))
        return free

    def run_esdump(self, query_commands):
        current_processes = set()
        max_processes = 2
        while len(query_commands) > 0:
            finished_processes = set()
            if len(current_processes) < max_processes:
                num_processes_to_run = min(
                    max_processes - len(current_processes), len(query_commands)
                )
                for i in range(num_processes_to_run):
                    current_query = query_commands.pop(0)
                    current_processes.add(
                        subprocess.Popen(
                            current_query, env={"NODE_TLS_REJECT_UNAUTHORIZED": "0"}
                        )
                    )
            for p in current_processes:
                if p.poll() is None:
                    p.wait()
                else:
                    finished_processes.add(p)
            current_processes -= finished_processes

    def retrieve_sample_logs(self):
        # Get the first 10k logs
        logging.info("Retrieve sample logs from ES")
        es_dump_cmd = (
            'elasticdump --searchBody \'{"query": { "match_all": {} }, "_source": ["masked_log", "timestamp"], "sort": [{"timestamp": {"order": "desc"}}]}\' --retryAttempts 10 --size=10000 --limit 10000 --input=%s/logs --output=%s --type=data'
            % (FORMATTED_ES_ENDPOINT, self.ES_DUMP_SAMPLE_LOGS_PATH)
        )
        subprocess.run(es_dump_cmd, shell=True)

        if os.path.exists(self.ES_DUMP_SAMPLE_LOGS_PATH):
            logging.info("Sampled downloaded successfully")
        else:
            logging.error("Sample failed to download")

    def calculate_training_logs_size(self, free):
        # Determine average size per log message
        sample_logs_bytes_size = os.path.getsize(self.ES_DUMP_SAMPLE_LOGS_PATH)
        num_lines = sum(1 for line in open(self.ES_DUMP_SAMPLE_LOGS_PATH))
        average_size_per_log_message = sample_logs_bytes_size / num_lines
        logging.info(f"average size per log message = {average_size_per_log_message} bytes")
        os.remove(self.ES_DUMP_SAMPLE_LOGS_PATH)
        # Determine maximum number of logs to fetch for training
        num_logs_to_fetch = int((free * 0.8) / average_size_per_log_message)
        logging.info(f"Maximum number of log messages to fetch = {num_logs_to_fetch}")
        return num_logs_to_fetch

    def get_log_count(self, es_instance, timestamps_list, num_logs_to_fetch):
        timestamps_esdump_num_logs_fetched = dict()
        total_number_of_logs = 0
        for timestamp_idx, timestamp_entry in enumerate(timestamps_list):
            start_ts, end_ts = timestamp_entry["start_ts"], timestamp_entry["end_ts"]
            query_body = {
                "query": {
                    "bool": {
                        "must": {"term": {"is_control_plane_log": "false"}},
                        "filter": [
                            {
                                "range": {
                                    "timestamp": {"gte": start_ts, "lte": end_ts}
                                }
                            }
                        ],
                    }
                }
            }
            try:
                num_entries = es_instance.count(index="logs", body=query_body)["count"]
                timestamps_esdump_num_logs_fetched[timestamp_idx] = num_entries
                total_number_of_logs += num_entries
            except Exception as e:
                logging.error(e)
                continue
        total_number_of_logs_to_fetch = min(num_logs_to_fetch, total_number_of_logs)
        if total_number_of_logs > 0:
            for idx_key in timestamps_esdump_num_logs_fetched:
                timestamps_esdump_num_logs_fetched[idx_key] /= total_number_of_logs
                timestamps_esdump_num_logs_fetched[
                    idx_key
                ] *= total_number_of_logs_to_fetch
                timestamps_esdump_num_logs_fetched[idx_key] = int(
                    timestamps_esdump_num_logs_fetched[idx_key]
                )

        return timestamps_esdump_num_logs_fetched

    def fetch_training_logs(self, es_instance, num_logs_to_fetch, timestamps_list):
        timestamps_esdump_num_logs_fetched = self.get_log_count(
            es_instance, timestamps_list, num_logs_to_fetch
        )
        # ESDump logs
        esdump_sample_command = [
            "elasticdump",
            "--searchBody",
            '{{"query": {{"bool": {{"must": [{{"term": {{"is_control_plane_log": false}}}},{{"range": {{"timestamp": {{"gte": {},"lt": {}}}}}}}]}}}} ,"_source": ["masked_log", "timestamp", "is_control_plane_log", "window_start_time_ns", "_id"], "sort": [{{"timestamp": {{"order": "desc"}}}}]}}',
            "--retryAttempts",
            "100",
            "--fileSize=50mb",
            "--size={}",
            "--limit",
            "10000",
            f"--input={FORMATTED_ES_ENDPOINT}/logs",
            "--output={}",
            "--type=data",
        ]
        query_queue = []
        for idx, entry in enumerate(timestamps_list):
            if timestamps_esdump_num_logs_fetched[idx] == 0:
                continue
            start_ts, end_ts = entry["start_ts"], entry["end_ts"]
            current_command = esdump_sample_command[:]
            current_command[2] = current_command[2].format(
                start_ts, end_ts
            )
            current_command[6] = current_command[6].format(
                timestamps_esdump_num_logs_fetched[idx]
            )
            current_command[10] = current_command[10].format(
                os.path.join(self.ES_DUMP_DIR, f"{start_ts}_{end_ts}.json")
            )
            query_queue.append(current_command)
        if len(query_queue) > 0:
            self.run_esdump(query_queue)
            return True
        else:
            return False


    def fetch_and_update_timestamps(self,es_instance):
        timestamps_list = []
        try:
            oldest_log = es_instance.search(index="logs", body={"aggs": {"min_ts": {"min": { "field": "timestamp"}}}, "_source": ["timestamp"]}, size=1)
        except Exception as e:
            logging.error(e)
            return timestamps_list
        oldest_log_timestamp = int(oldest_log["aggregations"]["min_ts"]["value"])
        try:
            all_normal_intervals = scan(es_instance, index="opni-normal-intervals", query={"query": {"match_all": {}}})
        except Exception as e:
            logging.error("Error trying to retrieve all normal intervals from opni-normal-intervals index")
            return timestamps_list
        for normal_interval in all_normal_intervals:
            start_ts, end_ts = normal_interval["_source"]["start_ts"], normal_interval["_source"]["end_ts"]
            file_prefix = "{}_{}".format(start_ts, end_ts)
            interval_json_files = [
                file
                for file in os.listdir(self.TRAINING_DIR)
                if file_prefix in file
            ]
            if end_ts < oldest_log_timestamp:
                try:
                    es_instance.delete(index="opni-normal-intervals", doc_type=normal_interval["_type"], id=normal_interval["_id"])
                    logging.info("Deleting old normal time interval from Elasticsearch")
                    for interval_file in interval_json_files:
                        os.remove(os.path.join(self.TRAINING_DIR,interval_file))
                except Exception as e:
                    logging.error("Error deleting document from opni-normal-intervals index.")
                    continue
            elif start_ts < oldest_log_timestamp:
                timestamps_list.append({"start_ts": oldest_log_timestamp, "end_ts": end_ts})
                try:
                    es_instance.update(index="opni-normal-intervals", doc_type=normal_interval["_type"], id=normal_interval["_id"], body={"doc": {"start_ts": oldest_log_timestamp}})
                    logging.info("Updating time interval within Elasticsearch.")
                    for interval_file in interval_json_files:
                        os.remove(os.path.join(self.TRAINING_DIR, interval_file))
                except Exception as e:
                    logging.error("Error updating document within opni-normal-intervals index.")
                    continue
            else:
                if len(interval_json_files) == 0:
                    timestamps_list.append({"start_ts": start_ts, "end_ts": end_ts})

        return timestamps_list


    def normalize_json_data(self):
        # For every json file, write/append each time window to own file
        for es_split_json_file in os.listdir(self.ES_DUMP_DIR):
            if not ".json" in es_split_json_file:
                continue
            json_file_to_process = os.path.join(
                self.ES_DUMP_DIR, es_split_json_file
            )
            df = pd.read_json(json_file_to_process, lines=True)
            df = pd.json_normalize(df["_source"])
            df[
                [
                    "timestamp",
                    "window_start_time_ns",
                    "masked_log",
                    "is_control_plane_log",
                ]
            ].to_json(
                os.path.join(
                    self.TRAINING_DIR,
                    "{}.json.gz".format(es_split_json_file.split(".json")[0]
                    ),
                ),
                orient="records",
                lines=True,
                compression="gzip",
            )
            # delete ESDumped file
            os.remove(json_file_to_process)
        shutil.rmtree(self.ES_DUMP_DIR)

    def run(self):
        if not os.path.exists(self.ES_DUMP_DIR):
            os.makedirs(self.ES_DUMP_DIR)

        if not os.path.exists(self.TRAINING_DIR):
            os.makedirs(self.TRAINING_DIR)
        es_instance = Elasticsearch(
            [ES_ENDPOINT],
            port=9200,
            http_auth=(ES_USERNAME, ES_PASSWORD),
            verify_certs=False,
            use_ssl=True,
        )
        free = self.disk_size()
        self.retrieve_sample_logs()
        num_logs_to_fetch = self.calculate_training_logs_size(free)
        timestamps_list = self.fetch_and_update_timestamps(es_instance)
        data_exists = self.fetch_training_logs(es_instance, num_logs_to_fetch, timestamps_list)
        if data_exists:
            self.normalize_json_data()
        return data_exists
