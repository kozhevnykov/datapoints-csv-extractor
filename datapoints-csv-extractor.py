import argparse
import logging
import os
import sys
import time
from logging.handlers import TimedRotatingFileHandler
from operator import itemgetter
from pathlib import Path

import pandas
from cognite import CogniteClient
from cognite.client.stable.datapoints import Datapoint, TimeseriesWithDatapoints

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s - %(message)s",
    handlers=[
        TimedRotatingFileHandler("extractor.log", when="midnight", backupCount=7),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger()


API_KEY = os.environ.get("COGNITE_EXTRACTOR_API_KEY")
if not API_KEY:
    print("COME ON JAN&SAM, YOU FORGOT THE API KEY!")
    sys.exit(2)


# Global variable for last timestamp processed
LAST_PROCESSED_TIMESTAMP = 1550076300

# Maximum number of time series batched at once
BATCH_MAX = 1000

# Path to folder of CSV files
FOLDER_PATH = "../TebisSampleData2/"


def post_datapoints(client, paths, existing_timeseries):
    current_time_series = []  # List of time series being processed

    def post_datapoints():
        nonlocal current_time_series
        client.datapoints.post_multi_time_series_datapoints(current_time_series)
        current_time_series = []

    def convert_float(value_str):
        return float(value_str.replace(",", "."))

    for path in paths:
        df = pandas.read_csv(path, encoding="latin-1", delimiter=";", quotechar='"', skiprows=[1], index_col=0)
        timestamps = [int(o) * 1000 for o in df.index.tolist()]
        count_of_data_points = 0

        for col in df:
            if len(current_time_series) >= BATCH_MAX:
                post_datapoints()

            name = str(col.rpartition(":")[2].strip())

            if name in existing_timeseries:
                data_points = []

                for i, value in enumerate(df[col].tolist()):
                    if pandas.notnull(value):
                        data_points.append(Datapoint(timestamp=timestamps[i], value=convert_float(value)))

                if data_points:
                    current_time_series.append(TimeseriesWithDatapoints(name=name, datapoints=data_points))
                    count_of_data_points += len(data_points)

        if current_time_series:
            post_datapoints()

        logger.info("Processed {} datapoints from {}".format(count_of_data_points, path))

    return max(path.stat().st_mtime for path in paths)  # Timestamp of most recent modified path


def find_new_files(last_mtime, base_path):
    paths = [(p, p.stat().st_mtime) for p in Path(base_path).glob("*.csv")]
    paths.sort(key=itemgetter(1), reverse=True)  # Process newest file first
    return [p for p, mtime in paths if mtime > last_mtime]


def extract_datapoints(data_type):
    client = CogniteClient(api_key=API_KEY)
    existing_timeseries = set(i["name"] for i in client.time_series.get_time_series(autopaging=True).to_json())

    if data_type == 'live':
        try:
            while True:
                paths = find_new_files(last_timestamp, FOLDER_PATH)
                if paths:
                    last_timestamp = post_datapoints(client, paths, existing_timeseries)

                    # logger.info("Removing processed files {}".format(', '.join(p.name for p in paths)))
                    # for path in paths:
                    #    path.unlink()

                    time.sleep(5)
        except KeyboardInterrupt:
            logger.warning("Extractor stopped")
    else if data_type == 'historical':
        paths = find_new_files(0, FOLDER_PATH) # All paths in folder, regardless of timestamp
        if paths:
            post_datapoints(client, paths, existing_timeseries)
        logger.info("Extraction complete")


# Ensure that user specifies live or historical data
def parse_arguments():
    parser = argparse.ArgumentParser()
    parser.add_argument('-d', '--data_type', choices=['live', 'historical'], type=str.lower, help='Input should be "live" or "historical" \
                to specify data type. If live data, the earliest time stamp to examine must be specified.')
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_arguments()
    extract_datapoints(args.data_type)
