#!/usr/bin/env python
# coding: utf-8
"""
A script that process csv files in a specified folder,
to extract data points to send to CDP.
"""
import argparse
import logging
import os
import sys
import time
from logging.handlers import TimedRotatingFileHandler
from operator import itemgetter
from pathlib import Path

import pandas
from cognite import APIError, CogniteClient
from cognite.client.stable.datapoints import Datapoint, TimeseriesWithDatapoints
from cognite.client.stable.time_series import TimeSeries
from cognite_prometheus.cognite_prometheus import CognitePrometheus

from prometheus import Prometheus

logger = logging.getLogger(__name__)

BATCH_MAX = 1000  # Maximum number of time series batched at once


def _parse_cli_args() -> None:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--live",
        "-l",
        action="store_true",
        help="By default, historical data will be processed. Use '--live' to process live data",
    )
    group.add_argument(
        "--historical", default=True, action="store_true", help="Process historical data instead of live"
    )
    parser.add_argument("--input", "-i", required=True, help="Folder path of the files to process")
    parser.add_argument("--timestamp", "-t", required=False, type=int, help="Optional, process files older than this")
    parser.add_argument("--log", "-d", required=False, default="log", help="Optional, log directory")
    parser.add_argument(
        "--move-failed",
        "-m",
        required=False,
        action="store_true",
        help="Optional, move failed csv files to subfolder failed",
    )
    parser.add_argument("--api-key", "-k", required=False, help="Optional, CDP API KEY")
    return parser.parse_args()


def _configure_logger(folder_path, live_processing: bool) -> None:
    """Create 'folder_path' and configure logging to file as well as console."""
    folder_path.mkdir(parents=True, exist_ok=True)
    log_file = folder_path.joinpath("extractor-{}.log".format("live" if live_processing else "historical"))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s - %(message)s",
        handlers=[
            TimedRotatingFileHandler(log_file, when="midnight", backupCount=7),
            logging.StreamHandler(sys.stdout),
        ],
    )


def _configure_prometheus(live: bool):
    """Configure prometheus object"""
    prometheus_jobname = os.environ.get("COGNITE_PROMETHEUS_JOBNAME")
    prometheus_username = os.environ.get("COGNITE_PROMETHEUS_USERNAME")
    prometheus_password = os.environ.get("COGNITE_PROMETHEUS_PASSWORD")

    try:
        CognitePrometheus(prometheus_jobname, prometheus_username, prometheus_password)
    except Exception as exc:
        logger.error("Failed to create Prometheus object: {!s}".format(exc))
        sys.exit(2)

    prometheus_object: CognitePrometheus = CognitePrometheus.get_prometheus_object()

    prometheus: Prometheus = Prometheus (
        prometheus = prometheus_object,
        live = live
    )

    return prometheus


def _log_error(func, *args, **vargs):
    """Call 'func' with args, then log if an exception was raised."""
    try:
        return func(*args, **vargs)
    except Exception as error:
        logger.info(error)


def create_data_points(values, timestamps):
    """Return CDP Datapoint object for 'values' and 'timestamps'."""
    data_points = []

    for i, value_string in enumerate(values):
        if pandas.notnull(value_string):
            try:
                value = float(value_string.replace(",", "."))
            except ValueError as error:
                logger.info(error)
            else:
                data_points.append(Datapoint(timestamp=timestamps[i], value=value))

    return data_points


def process_csv_file(client, prometheus, csv_path, existing_time_series) -> None:
    """Find datapoints inside a single csv file and send it to CDP."""
    count_of_data_points = 0
    current_time_series = []  # List of time series being processed

    df = pandas.read_csv(csv_path, encoding="latin-1", delimiter=";", quotechar='"', skiprows=[1], index_col=0)
    timestamps = [int(o) * 1000 for o in df.index.tolist()]

    for col in df:
        if len(current_time_series) >= BATCH_MAX:
            _log_error(client.datapoints.post_multi_time_series_datapoints, current_time_series)
            current_time_series.clear()

        name = col.rpartition(":")[2].strip()
        external_id = col.rpartition(":")[0].strip()

        if external_id not in existing_time_series:
            new_time_series = TimeSeries(
                name=name,
                description="Auto-generated time series, external ID not found",
                metadata={"externalID": external_id},
            )
            _log_error(client.time_series.post_time_series, [new_time_series])
            existing_time_series[external_id] = name

            prometheus.time_series_gauge.labels(data_type=prometheus.data_type).inc()
            prometheus.prometheus.push_to_server()

        data_points = create_data_points(df[col].tolist(), timestamps)
        if data_points:
            current_time_series.append(
                TimeseriesWithDatapoints(name=existing_time_series[external_id], datapoints=data_points)
            )
            count_of_data_points += len(data_points)
            prometheus.time_series_data_points_gauge.labels(data_type=prometheus.data_type, external_id=external_id).inc(len(data_points))

    if current_time_series:
        _log_error(client.datapoints.post_multi_time_series_datapoints, current_time_series)

    logger.info("Processed {} datapoints from {}".format(count_of_data_points, csv_path))

    prometheus.all_data_points_gauge.labels(data_type=prometheus.data_type).inc(count_of_data_points)
    prometheus.prometheus.push_to_server()
    sys.exit(2)


def process_files(client, prometheus, paths, time_series_cache, failed_path) -> None:
    """Process one csv file at a time, and either delete it or possibly move it when done."""
    for path in paths:
        try:
            try:
                process_csv_file(client, prometheus, path, time_series_cache)
            except Exception as exc:
                logger.error("Parsing of file {} failed: {!s}".format(path, exc))
                if failed_path is not None:
                    failed_path.mkdir(parents=True, exist_ok=True)
                    path.replace(failed_path.joinpath(path.name))
            else:
                path.unlink()
        except IOError as exc:
            logger.error("Failed to delete/move file {}: {!s}".format(path, exc))


def find_files_in_path(folder_path, after_timestamp: int, limit: int = None, newest_first: bool = True):
    """Return csv files in 'folder_path' sorted by 'newest_first'."""
    before_timestamp = int(time.time() - 2)  # Process files more than 2 seconds old
    all_relevant_paths = []

    for path in folder_path.glob("*.csv"):
        try:
            modified_timestamp = path.stat().st_mtime
        except IOError as exc:  # Possible that file no longer exists
            logger.error("Failed to find stats on file {!s}: {!s}".format(path, exc))
            continue
        if after_timestamp < modified_timestamp < before_timestamp:
            all_relevant_paths.append(path)

    paths = sorted(all_relevant_paths, reverse=newest_first)
    return paths if not limit else paths[:limit]


def get_all_time_series(client):
    """Return map of timeseries externalId -> name of all timeseries that has externalId."""
    for i in range(10):
        try:
            res = client.time_series.get_time_series(include_metadata=True, autopaging=True)
        except APIError as exc:
            logger.error("Failed to get timeseries: {!s}".format(exc))
            time.sleep(i)
        else:
            break
    else:
        logger.fatal("Could not fetch time series data from CDP, exiting!")
        sys.exit(1)

    return {i["metadata"]["externalID"]: i["name"] for i in res.to_json() if "externalID" in i["metadata"]}


def extract_data_points(client, prometheus, time_series_cache, live_mode: bool, start_timestamp: int, folder_path, failed_path):
    """Find datapoints in files in 'folder_path' and send them to CDP."""
    try:
        if live_mode:
            while True:
                paths = find_files_in_path(folder_path, start_timestamp, limit=20)
                if paths:
                    process_files(client, prometheus, paths, time_series_cache, failed_path)
                time.sleep(3)

        else:
            paths = find_files_in_path(folder_path, start_timestamp, newest_first=False)
            if paths:
                process_files(client, prometheus, paths, time_series_cache, failed_path)
            else:
                logger.info("Found no files to process in {}".format(folder_path))
        logger.info("Extraction complete")
    except KeyboardInterrupt:
        logger.warning("Extractor stopped")


def main(args):
    _configure_logger(Path(args.log), args.live)
    prometheus = _configure_prometheus(args.live)

    api_key = args.api_key if args.api_key else os.environ.get("COGNITE_EXTRACTOR_API_KEY")
    args.api_key = ""  # Don't log the api key if given through CLI
    logger.info("Extractor configured with {}".format(args))
    start_timestamp = args.timestamp if args.timestamp else 0

    input_path = Path(args.input)
    if not input_path.exists():
        logger.fatal("Input folder does not exists: {!s}".format(input_path))
        sys.exit(2)
    failed_path = input_path.joinpath("failed") if args.move_failed else None

    try:
        client = CogniteClient(api_key=api_key)
        client.login.status()
    except APIError as exc:
        logger.error("Failed to create CDP client: {!s}".format(exc))
        client = CogniteClient(api_key=api_key)

    extract_data_points(client, prometheus, get_all_time_series(client), args.live, start_timestamp, input_path, failed_path)

if __name__ == "__main__":
    main(_parse_cli_args())
