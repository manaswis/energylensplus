"""
[CONSUMER] Processes the data generated by the producer

1. Detect edges in real-time over streaming power data
2. Initiates the classification pipeline
3. Detects wastage/usage
4. Sends real-time feedback to user
"""

from __future__ import absolute_import

import os
import time
import pandas as pd
import datetime as dt
from multiprocessing.managers import BaseManager

from celery import shared_task

# Imports from EnergyLens+
from energylenserver.preprocessing import wifi
from energylenserver.preprocessing import functions as pre_f
from energylenserver.wifi import functions as wifi_f
from energylenserver.models.DataModels import *
from energylenserver.models.models import *
from energylenserver.meter.edge_detection import detect_and_filter_edges
from energylenserver.gcmxmppclient.messages import create_message
from energylenserver.constants import ENERGY_WASTAGE_NOTIF_API


# Global variables
# Model mapping with filenames

FILE_MODEL_MAP = {
    'wifi': WiFiTestData,
    'rawaudio': RawAudioTestData,
    'audio': MFCCFeatureTestSet,
    'accelerometer': AcclTestData,
    'light': LightTestData,
    'mag': MagTestData,
    'Trainingwifi': WiFiTrainData,
    'Trainingrawaudio': RawAudioTrainData,
    'Trainingaudio': MFCCFeatureTrainSet,
    'Trainingaccelerometer': AcclTrainData,
    'Traininglight': LightTrainData,
    'Trainingmag': MagTrainData
}


class ClientManager(BaseManager):
    pass

# Establishing connection with the running gcmserver
try:
    ClientManager.register('get_client')
    manager = ClientManager(address=('localhost', 50000), authkey='abracadabra')
    manager.connect()
    client = manager.get_client()
except Exception, e:
    pass

"""
Data Handlers
"""


@shared_task
def phoneDataHandler(filename, sensor_name, df_csv, training_status, dev_id):
    """
    Consumes sensor data streams from the phone
    Performs some preprocessing and inserts records
    into the database

    Currently, saves in the database, record by record
    TODO: Modify to save csv in one operation - using
    raw sql command

    :param filename:
    :param sensor_name:
    :param df_csv:
    :param training_status:
    :param dev_id:
    :return upload status:
    """

    print "\n---Starting Insertion of Records for " + filename + " ---"

    # --Preprocess records before storing--
    if sensor_name == 'wifi':
        df_csv = wifi.format_data(df_csv)
        if df_csv is False:
            print "Incorrect file sent. Upload not successful!"
            return False

    # Remove NAN timestamps
    df_csv.dropna(subset=[0], inplace=True)
    # Remove rows with 'Infinity' in MFCCs created
    if sensor_name == 'audio':
        if str(df_csv.mfcc1.dtype) != 'float64':
            df_csv = df_csv[df_csv.mfcc1 != '-Infinity']

    print "Total number of records to insert: " + str(len(df_csv))

    # --Initialize Model--
    if training_status is True:
        model = FILE_MODEL_MAP['Training' + sensor_name]
    else:
        model = FILE_MODEL_MAP[sensor_name]

    # --Store data in the model--
    print "Inserting records..."
    for idx in df_csv.index:
        record = list(df_csv.ix[idx])
        model().save_data(dev_id, record)
    now_time = "[" + time.ctime(time.time()) + "]"
    print now_time + " Successful Upload for " + sensor_name + " " + filename + "!!\n"

    return True


@shared_task
def meterDataHandler(df, file_path):
    """
    Consumes sensor data streams from the meter
    """

    meter_uuid_folder = os.path.dirname(file_path)
    uuid = meter_uuid_folder.split('/')[-1]
    print "Detecting Edges for UUID:: " + uuid

    # -- Detect Edge --
    edges_df = detect_and_filter_edges(df)
    # Store edges into db

    # For the detected edge, store edge and call classification pipeline task
    for idx in edges_df.index:
        edge = edges_df.ix[idx]
        edge_time = edge.time

        try:

            meter = MeterInfo.objects.get(meter_uuid__exact=uuid)
            # Check if the edge exists
            try:
                record = Edges.objects.get(meter__exact=meter, timestamp__exact=edge_time)
            except Edges.DoesNotExist, e:

                # --Store edge--
                edge_r = Edges(timestamp=int(edge_time), time=dt.datetime.fromtimestamp(edge_time),
                               magnitude=edge.magnitude, type=edge.type,
                               curr_power=edge.curr_power, meter=meter)
                edge_r.save()

                print("Edge for UUID: " + uuid + " at [" + time.ctime(edge['time']) + "] of mag "
                      + str(edge['magnitude']))

                # Initiate classification pipeline
                edgeHandler(edge_r)
        except Exception, e:
            print "[EdgeSaveException]:: " + str(e)


def edgeHandler(edge):
    """
    Starts the classification pipeline and relays edges based on edge type
    """
    print "Starting the Classification pipeline.."
    if edge.type == 'falling':
        chain = classifyEdgeHandler.s(edge) | findTimeSliceHandler.s(edge)
    else:
        chain = classifyEdgeHandler.s(edge) | determineWastageHandler.s(edge)
    chain()
    print "Classification Pipeline ended!"

"""
Invokes the EnergyLens+ core algorithm
"""


@shared_task
def classifyEdgeHandler(edge):
    """
    Consumes smart meter edges and phone data to give out 'who', 'what', 'where' and 'when''
    :param edge:
    :return "where", what" and "who" labels:
    """
    print("Classify edge of type: " + edge.type +
          ": [" + time.ctime(edge.timestamp) + "] :: " + str(edge.magnitude))
    # TODO TODAY!!
    # Preprocessing Step 2: Determine user at home
    at_home, user_list = wifi_f.determine_user_home_status(edge.timestamp)
    if not at_home:
        return '', '', ''
    # Preprocessing Step 3: Determine phone is with user
    phone_with_user = pre_f.determine_phone_with_user(edge.timestamp)
    if not phone_with_user:
        return '', '', ''
    who = 'Manaswi'
    where = 'Bedroom'
    what = 'Laptop'
    time.sleep(2)
    print "[" + time.ctime(edge.timestamp) + "] :: Determined labels:" + who + " " + where + " " + what
    # Using id as a reference and store the who what where

    return who, what, where


@shared_task
def findTimeSliceHandler(result_labels, edge):
    """
    Consumes "where", what" and "who" labels and gives out "when"
    Runs when an OFF event is detected
    :param edge:
    :param who:
    :param what:
    :param where:
    :return when:
    """
    who, what, where = result_labels
    print "Determines activity duration: [" + time.ctime(edge.timestamp) + "] :: " + str(edge.magnitude)
    time.sleep(2)
    start_time = time.ctime(edge.timestamp - 10)
    end_time = time.ctime(edge.timestamp)
    print("[" + time.ctime(edge.timestamp) + "] :: Time slice for activity: "
          + who + " uses " + what + " in " + where + " during " + start_time + " and " + end_time)

    return start_time, end_time

"""
Invokes the components that use EnergyLens+ outputs - who, what, where and when:
1. Wastage Detection
2. Energy Apportionment
"""


@shared_task
def determineWastageHandler(result_label, edge):
    """
    Consumes edge and determines energy wastage
    :param edge:
    :return determined wastage:
    """
    energy_wasted = True
    who, what, where = result_label
    reg_id = ''  # Get regid based on the who value

    print "Determines energy wastage:: [" + time.ctime(edge.timestamp) + "] :: " + str(edge.magnitude)
    print "Activity: " + who + " in " + where + " uses " + what
    # Call module that determines energy wastage
    time.sleep(2)

    if energy_wasted:
        # Call real-time feedback component to send a message to the user
        message_to_send = {}
        message_to_send['msg_type'] = 'response'
        message_to_send['api'] = ENERGY_WASTAGE_NOTIF_API
        message_to_send['options'] = {}
        message_to_send['options']['message'] = 'Please turn off the Light in the Bedroom'
        inform_user.delay(edge, reg_id, message_to_send)
        pass

    print "Wastage Determined: " + str(energy_wasted)

    return energy_wasted


@shared_task
def apportion_energy():
    """
    Determines the energy usage of an individual based on activity parameters
    and length of stay for each activity

    :return: energy usage
    """
    print "Apportioning energy.."


"""
Invokes the real-time feedback component
"""


@shared_task
def inform_user(edge, reg_id, message_to_send):
    """
    Informs the user by sending a notification to the phone
    :return message:
    """
    print "Sending message:: [" + time.ctime(edge.timestamp) + "] :: " + str(edge.magnitude)
    # Call module that sends message
    message = create_message(reg_id, message_to_send)
    client.send_message(message)
    print "Message Sent [" + time.ctime(edge.timestamp) + "] :: " + str(edge.magnitude)

"""
Test Task
"""


@shared_task
def send_msg(reg_id):
    print "Sending Message [" + time.ctime(time.time()) + "]"
    message_to_send = {}
    message_to_send['msg_type'] = 'response'
    message_to_send['api'] = ENERGY_WASTAGE_NOTIF_API
    message_to_send['options'] = {}
    message_to_send['options']['message'] = 'Please turn off the Light in the Bedroom'
    message = create_message(reg_id, message_to_send)
    client.send_message(message)
    print "Message Sent [" + time.ctime(time.time()) + "]"


'''
Test Tasks
@shared_task
def test_task(x):
    print "X=" + str(x)
    relay(x)


def relay(x):
    print "Called relay"
    chain = add.s(x, 10) | mul.s(1001)
    chain()

@shared_task
def add(x,y):
    print "Called Add"
    print "x="+ str(x) + " y=" + str(y)
    z = x+y
    return x,y,z

@shared_task
def mul(s, n):
    print "Called mul"
    print n
    print s
    (x,y,z) = s
    print "x="+ str(x) + " y=" + str(y) + " z=" + str(z)
    w = x * y * z
    print "W=" + w
'''