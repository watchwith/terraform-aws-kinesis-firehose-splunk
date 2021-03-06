"""
For processing data sent to Firehose by Cloudwatch Logs subscription filters.

Cloudwatch Logs sends to Firehose records that look like this:

{
  "messageType": "DATA_MESSAGE",
  "owner": "123456789012",
  "logGroup": "log_group_name",
  "logStream": "log_stream_name",
  "subscriptionFilters": [
    "subscription_filter_name"
  ],
  "logEvents": [
    {
      "id": "01234567890123456789012345678901234567890123456789012345",
      "timestamp": 1510109208016,
      "message": "log message 1"
    },
    {
      "id": "01234567890123456789012345678901234567890123456789012345",
      "timestamp": 1510109208017,
      "message": "log message 2"
    }
    ...
  ]
}

The data is additionally compressed with GZIP.

The code below will:

1) Gunzip the data
2) Parse the json
3) Set the result to ProcessingFailed for any record whose messageType is not DATA_MESSAGE, thus redirecting them to the
   processing error output. Such records do not contain any log events. You can modify the code to set the result to
   Dropped instead to get rid of these records completely.
4) For records whose messageType is DATA_MESSAGE, extract the individual log events from the logEvents field, and pass
   each one to the transformLogEvent method. You can modify the transformLogEvent method to perform custom
   transformations on the log events.
5) Concatenate the result from (4) together and set the result as the data of the record returned to Firehose. Note that
   this step will not add any delimiters. Delimiters should be appended by the logic within the transformLogEvent
   method.
6) Any additional records which exceed 6MB will be re-ingested back into Firehose.

"""

import os
import base64
import json
import gzip
from io import BytesIO, BufferedReader
import boto3
import logging
import datetime

logger = logging.getLogger()
logger.setLevel(os.getenv("LOG_LEVEL", "INFO"))
logging.getLogger('boto3').setLevel(logging.CRITICAL)
logging.getLogger('botocore').setLevel(logging.CRITICAL)
logging.getLogger('s3transfer').setLevel(logging.CRITICAL)
logging.getLogger('urllib3').setLevel(logging.CRITICAL)

maxSize = int(os.getenv("MAXSIZE", "9900000"))


def isgzip(stream):
    buf = stream.peek(3)
    return buf[0] == 0x1F and buf[1] == 0x8B and buf[2] == 0x08


def transformLogEvent(log_event, owner, group, stream):
    """Transform each log event.

    The default implementation below just extracts the message and appends a newline to it.

    Args:
    log_event (dict): The original log event. Structure is {"id": str, "timestamp": long, "message": str}

    Returns:
    str: The transformed log event.
    """
    log_event["owner"] = owner
    log_event["log_group"] = group
    log_event["log_stream"] = stream
    log_event = addTimestamp(log_event)
    log_event = addEventWrapper(log_event)
    return json.dumps(log_event) + "\n"


def addTimestamp(event):
    if "timestamp" not in event:
        ts = {
            "timestamp": datetime.datetime.utcnow()
            .replace(tzinfo=datetime.timezone.utc)
            .strftime("%Y-%m-%dT%X.%fZ")
        }
        ts.update(event)
        event = ts
    return event


def addEventWrapper(event):
    ev = {}
    ev["event"] = event
    return ev


def processRecords(records):
    for r in records:
        rawdata = base64.b64decode(r["data"])
        bytesiodata = BufferedReader(BytesIO(rawdata))
        if isgzip(bytesiodata):
            doc = gzip.GzipFile(fileobj=bytesiodata, mode="r").read().decode("utf-8")
        else:
            doc = bytesiodata.read().decode("utf-8")

        recId = r["recordId"]
        logger.info("processing doc, recordId={} size={}".format(recId, len(doc)))
        logger.debug("doc: " + doc)
        """
        CONTROL_MESSAGE are sent by CWL to check if the subscription is reachable.
        They do not contain actual data.
        """
        try:
            data = json.loads(doc)
        except json.decoder.JSONDecodeError:
            plaintext = {}
            plaintext = addTimestamp(plaintext)
            plaintext["message"] = doc
            message = json.dumps(addEventWrapper(plaintext))
            logger.info("plaintext size={}".format(len(message)))
            logger.debug("plaintext: " + message)
            yield {
                "data": base64.b64encode((message + "\n").encode("utf-8")),
                "result": "Ok",
                "recordId": recId,
            }
            continue

        if "messageType" in data:
            if data["messageType"] == "CONTROL_MESSAGE":
                yield {"result": "Dropped", "recordId": recId}
            elif data["messageType"] == "DATA_MESSAGE":
                message = "".join(
                    [
                        transformLogEvent(
                            e, data["owner"], data["logGroup"], data["logStream"]
                        )
                        for e in data["logEvents"]
                    ]
                )
                message = base64.b64encode(message.encode("utf-8"))
                yield {"data": message, "result": "Ok", "recordId": recId}
            else:
                yield {"result": "ProcessingFailed", "recordId": recId}
        elif "container_id" in data and "log" in data:
            try:
                logdata = json.loads(data["log"])
                data.update(logdata)
                del data["log"]
            except json.decoder.JSONDecodeError:
                pass
            message = json.dumps(addEventWrapper(data)) + "\n"
            message = base64.b64encode(message.encode("utf-8"))
            yield {"data": message, "result": "Ok", "recordId": recId}
        else:
            message = json.dumps(addEventWrapper(data)) + "\n"
            message = base64.b64encode(message.encode("utf-8"))
            yield {"data": message, "result": "Ok", "recordId": recId}


def putRecordsToFirehoseStream(streamName, records, client, attemptsMade, maxAttempts):
    logger.debug(
        "putRecordsToFirehoseStream: streamName={} cntOfRecords={} attemptsMade={} maxAttempts={}".format(
            streamName, len(records), attemptsMade, maxAttempts
        )
    )
    failedRecords = []
    codes = []
    errMsg = ""
    # if put_record_batch throws for whatever reason, response['xx'] will error out, adding a check for a valid
    # response will prevent this
    response = None
    try:
        response = client.put_record_batch(
            DeliveryStreamName=streamName, Records=records
        )
    except Exception as e:
        failedRecords = records
        errMsg = str(e)

    # if there are no failedRecords (put_record_batch succeeded), iterate over the response to gather results
    if not failedRecords and response and response["FailedPutCount"] > 0:
        for idx, res in enumerate(response["RequestResponses"]):
            # (if the result does not have a key 'ErrorCode' OR if it does and is empty) => we do not need to re-ingest
            if "ErrorCode" not in res or not res["ErrorCode"]:
                continue

            codes.append(res["ErrorCode"])
            failedRecords.append(records[idx])

        errMsg = "Individual error codes: " + ",".join(codes)

    if len(failedRecords) > 0:
        if attemptsMade + 1 < maxAttempts:
            logger.error(
                "Some records failed while calling PutRecordBatch to Firehose stream, retrying. %s"
                % (errMsg)
            )
            putRecordsToFirehoseStream(
                streamName, failedRecords, client, attemptsMade + 1, maxAttempts
            )
        else:
            raise RuntimeError(
                "Could not put records after %s attempts. %s"
                % (str(maxAttempts), errMsg)
            )


def putRecordsToKinesisStream(streamName, records, client, attemptsMade, maxAttempts):
    logger.debug(
        "putRecordsToKinesisStream: streamName={} cntOfRecords={} attemptsMade={} maxAttempts={}".format(
            streamName, len(records), attemptsMade, maxAttempts
        )
    )
    failedRecords = []
    codes = []
    errMsg = ""
    # if put_records throws for whatever reason, response['xx'] will error out, adding a check for a valid
    # response will prevent this
    response = None
    try:
        response = client.put_records(StreamName=streamName, Records=records)
    except Exception as e:
        failedRecords = records
        errMsg = str(e)

    # if there are no failedRecords (put_record_batch succeeded), iterate over the response to gather results
    if not failedRecords and response and response["FailedRecordCount"] > 0:
        for idx, res in enumerate(response["Records"]):
            # (if the result does not have a key 'ErrorCode' OR if it does and is empty) => we do not need to re-ingest
            if "ErrorCode" not in res or not res["ErrorCode"]:
                continue

            codes.append(res["ErrorCode"])
            failedRecords.append(records[idx])

        errMsg = "Individual error codes: " + ",".join(codes)

    if len(failedRecords) > 0:
        if attemptsMade + 1 < maxAttempts:
            logger.error(
                "Some records failed while calling PutRecords to Kinesis stream, retrying. %s"
                % (errMsg)
            )
            putRecordsToKinesisStream(
                streamName, failedRecords, client, attemptsMade + 1, maxAttempts
            )
        else:
            raise RuntimeError(
                "Could not put records after %s attempts. %s"
                % (str(maxAttempts), errMsg)
            )


def createReingestionRecord(isSas, originalRecord):
    if isSas:
        return {
            "data": base64.b64decode(originalRecord["data"]),
            "partitionKey": originalRecord["kinesisRecordMetadata"]["partitionKey"],
        }
    else:
        return {"data": base64.b64decode(originalRecord["data"])}


def getReingestionRecord(isSas, reIngestionRecord):
    if isSas:
        return {
            "Data": reIngestionRecord["data"],
            "PartitionKey": reIngestionRecord["partitionKey"],
        }
    else:
        return {"Data": reIngestionRecord["data"]}


def handler(event, context):
    isSas = "sourceKinesisStreamArn" in event
    streamARN = event["sourceKinesisStreamArn"] if isSas else event["deliveryStreamArn"]
    region = streamARN.split(":")[3]
    streamName = streamARN.split("/")[1]
    records = list(processRecords(event["records"]))
    projectedSize = 0
    dataByRecordId = {
        rec["recordId"]: createReingestionRecord(isSas, rec) for rec in event["records"]
    }
    putRecordBatches = []
    recordsToReingest = []
    totalRecordsToBeReingested = 0

    for idx, rec in enumerate(records):
        logger.debug("Record: %s" % (rec))
        if rec["result"] != "Ok":
            continue
        projectedSize += len(rec["data"]) + len(rec["recordId"])
        # Original code set this to 6000000, see note below:
        # 6000000 instead of 6291456 to leave ample headroom for the stuff we didn't account for
        if projectedSize > maxSize:
            logger.debug(
                "Projected size {} exceeded {}, adding to reingest".format(
                    projectedSize, maxSize
                )
            )
            totalRecordsToBeReingested += 1
            recordsToReingest.append(
                getReingestionRecord(isSas, dataByRecordId[rec["recordId"]])
            )
            records[idx]["result"] = "Dropped"
            del records[idx]["data"]

        # split out the record batches into multiple groups, 500 records at max per group
        if len(recordsToReingest) == 500:
            logger.debug("Reingest batch at max, pushing to stream")
            putRecordBatches.append(recordsToReingest)
            recordsToReingest = []

    if len(recordsToReingest) > 0:
        # add the last batch
        logger.debug(
            "Reingest queue not empty, pushing {} records to stream".format(
                len(recordsToReingest)
            )
        )
        putRecordBatches.append(recordsToReingest)

    # iterate and call putRecordBatch for each group
    recordsReingestedSoFar = 0
    if len(putRecordBatches) > 0:
        client = (
            boto3.client("kinesis", region_name=region)
            if isSas
            else boto3.client("firehose", region_name=region)
        )
        for recordBatch in putRecordBatches:
            if isSas:
                putRecordsToKinesisStream(
                    streamName, recordBatch, client, attemptsMade=0, maxAttempts=20
                )
            else:
                putRecordsToFirehoseStream(
                    streamName, recordBatch, client, attemptsMade=0, maxAttempts=20
                )
            recordsReingestedSoFar += len(recordBatch)
            logger.info(
                "Reingested %d/%d records out of %d"
                % (
                    recordsReingestedSoFar,
                    totalRecordsToBeReingested,
                    len(event["records"]),
                )
            )
    else:
        logger.info("No records to be reingested")

    logger.debug("Returning {} records".format(len(records)))
    return {"records": records}
