"""
[CONSUMER] Processes the data generated by the producer

1. Detect edges in real-time over streaming power data
2. Initiates the classification pipeline
3. Detects wastage/usage
4. Sends real-time feedback to user
"""

from __future__ import absolute_import

from energylenserver.setup_django_envt import *

import os
import time
import pandas as pd
import datetime as dt
from multiprocessing.managers import BaseManager

from celery import shared_task

# Imports from EnergyLens+
from energylenserver.core import functions as core_f
from energylenserver.models.DataModels import *
from energylenserver.models.models import *
from energylenserver.models import functions as mod_func
from energylenserver.meter.edge_detection import detect_and_filter_edges
from energylenserver.gcmxmppclient.messages import create_message
from energylenserver.constants import ENERGY_WASTAGE_NOTIF_API, GROUND_TRUTH_NOTIF_API
from energylenserver.api import reporting as rpt


# Global variables
# Model mapping with filenames

FILE_MODEL_MAP = {
    'wifi': (WiFiTestData, "WiFiTestData"),
    'rawaudio': (RawAudioTestData, "RawAudioTestData"),
    'audio': (MFCCFeatureTestSet, "MFCCFeatureTestSet"),
    'accelerometer': (AcclTestData, "AcclTestData"),
    'light': (LightTestData, "LightTestData"),
    'mag': (MagTestData, "MagTestData"),
    'Trainingwifi': (WiFiTrainData, "WiFiTrainData"),
    'Trainingrawaudio': (RawAudioTrainData, "RawAudioTrainData"),
    'Trainingaudio': (MFCCFeatureTrainSet, "MFCCFeatureTrainSet"),
    'Trainingaccelerometer': (AcclTrainData, "AcclTrainData"),
    'Traininglight': (LightTrainData, "LightTrainData"),
    'Trainingmag': (MagTrainData, "MagTrainData")
}


class ClientManager(BaseManager):
    pass

# Establishing connection with the running gcmserver
try:
    ClientManager.register('get_msg_client')
    manager = ClientManager(address=('localhost', 50000), authkey='abracadabra')
    manager.connect()
    client = manager.get_msg_client()
except Exception, e:
    pass

"""
Data Handlers
"""


@shared_task
def phoneDataHandler(filename, sensor_name, filepath, training_status, user):
    """
    Consumes sensor data streams from the phone
    Performs some preprocessing and inserts records
    into the database

    :param filename:
    :param sensor_name:
    :param df_csv:
    :param training_status:
    :param user:
    :return upload status:
    """

    now_time = "[" + time.ctime(time.time()) + "]"
    print now_time + " FILE:: " + filename

    # Create a dataframe for preprocessing
    if sensor_name != 'rawaudio':
        try:
            df_csv = pd.read_csv(filepath)
        except Exception, e:
            print "[InsertDataException]!::", str(e)
            os.remove(filepath)
            return

    # Remove rows with 'Infinity' in MFCCs created
    if sensor_name == 'audio':
        if str(df_csv.mfcc1.dtype) != 'float64':
            df_csv = df_csv[df_csv.mfcc1 != '-Infinity']

    if sensor_name != 'rawaudio':
        print "Total number of records to insert: " + str(len(df_csv))

        # Remove NAN timestamps
        df_csv.dropna(subset=[0], inplace=True)

        # Create temp csv file
        os.remove(filepath)
        df_csv.to_csv(filepath, index=False)

    # --Initialize Model--
    if training_status is True:
        model = FILE_MODEL_MAP['Training' + sensor_name]
    else:
        model = FILE_MODEL_MAP[sensor_name]

    # --Store data in the model--
    model[0]().insert_records(user, filepath, model[1])

    now_time = "[" + time.ctime(time.time()) + "]"
    print now_time + " Successful Upload for " + sensor_name + " " + filename + "!!\n"


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
    if edge.type == "falling":
        chain = classifyEdgeHandler.s(
            edge) | findTimeSliceHandler.s() | determineWastageHandler.s()
    else:
        chain = classifyEdgeHandler.s(edge) | determineWastageHandler.s()
    chain()
    print("Classification Pipeline ended for edge: [%s] :: %d" % (
        time.ctime(edge.timestamp), edge.magnitude))

"""
Invokes the EnergyLens+ core algorithm
"""


@shared_task
def classifyEdgeHandler(edge):
    """
    Consumes smart meter edges and phone data to give out 'who', 'what', 'where' and 'when'
    :param edge:
    :return "where", what" and "who" labels:
    """
    apt_no = edge.meter.apt_no
    print("Apt.No.:: %d Classify edge of type: '%s' : [%s] :: %d" % (
        apt_no, edge.type, time.ctime(edge.timestamp), edge.magnitude))

    # --- Preprocessing ---
    # Step 2: Determine user at home
    user_list = core_f.determine_user_home_status(edge.timestamp, apt_no)
    if len(user_list) == 0:
        return 'ignore', 'ignore', 'ignore'

    # --- Classification ---
    # Step 1: Determine location for every user
    location = classify_location(edge.timestamp, user_list)

    # Step 2: Determine appliance for every user
    appliance = clasify_sound(edge.timestamp, user_list)

    # Step 3: Determine user based on location, appliance and metadata
    user = determine_user(location, appliance, user_list)

    who = user['dev_id']
    where = user['location']
    what = user['appliance']

    if edge.type == "rising":
        event_type = "ON"
    else:
        event_type = "OFF"

    print("[%s] :: Determined labels: %s %s %s" % (time.ctime(edge.timestamp), who, where, what))

    # Create a record in the Event Log with edge id
    # and store who what where labels
    event = EventLog(edge_id=edge, event_time=edge.timestamp,
                     location=where, appliance=what, dev_id=who, event_type=event_type)
    event.save()

    return who, what, where, event


@shared_task
def findTimeSliceHandler(result_labels):
    """
    Consumes "where", what" and "who" labels and gives out "when"
    and stores an "activity"
    Runs when an OFF event is detected
    :param edge:
    :param who:
    :param what:
    :param where:
    :return when:
    """
    who, what, where, event = result_labels
    print("Determines activity duration: [%s] :: %s" % (
        time.ctime(edge.timestamp), str(edge.magnitude)))

    if who == 'ignore' and what == 'ignore' and where == 'ignore':
        return
    time.sleep(2)
    start_time = time.ctime(edge.timestamp - 10)
    end_time = time.ctime(edge.timestamp)
    print("[" + time.ctime(edge.timestamp) + "] :: Time slice for activity: "
          + who + " uses " + what + " in " + where + " during " + start_time + " and " + end_time)

    return start_time, end_time, event

"""
Invokes the components that use EnergyLens+ outputs - who, what, where and when:
1. Wastage Detection
2. Energy Apportionment
"""


@shared_task
def determineWastageHandler(result_labels):
    """
    Consumes edge and determines energy wastage
    :param edge:
    :return determined wastage:
    """
    energy_wasted = True
    who, what, where, event = result_labels
    reg_id = ''  # Get regid based on the who value

    print("Determines energy wastage:: [%s] :: %s" % (
        time.ctime(edge.timestamp), str(edge.magnitude)))
    print "Activity: " + who + " in " + where + " uses " + what
    # Call module that determines energy wastage
    time.sleep(2)

    if energy_wasted:
        # Call real-time feedback component to send a message to the user
        message_to_send = {}
        message_to_send['msg_type'] = 'response'
        message_to_send['api'] = ENERGY_WASTAGE_NOTIF_API
        message_to_send['options'] = {}
        message_to_send['options']['message'] = 'Please turn off the Light in the Bedroom.'
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


@shared_task(name='tasks.send_validation_report')
def send_validation_report():
    """
    Sends a ground truth validation report to all the users
    """
    print "Sending periodic validation report.."
    # Get all the users
    users = mod_func.get_all_users()
    if users is False:
        print "No users that are active"
        return
    for user in users:
        reg_id = user.reg_id
        apt_no = user.apt_no
        dev_id = user.dev_id

        # Construct the message
        data_to_send = {}
        data_to_send['msg_type'] = "response"
        data_to_send['api'] = GROUND_TRUTH_NOTIF_API
        data_to_send['options'] = {}
        activities = rpt.get_inferred_activities(dev_id)

        if len(activities) <= 0:
            return

        appliances = mod_func.retrieve_metadata(apt_no)

        users = mod_func.retrieve_users(apt_no)

        occupants = {}
        for user_i in users:
            occupants[user_i.dev_id] = user_i.name

        data_to_send['options']['activities'] = activities
        data_to_send['options']['appliances'] = appliances
        data_to_send['options']['occupants'] = occupants

        message = create_message(reg_id, data_to_send)

        # Send the message to all the users
        client.send_message(message)

        print "Sending report for:: " + user.name


@shared_task
def send_wastage_notification(apt_no):
    """
    Sends a wastage notification to all the users
    """
    import random
    import string

    # Get all the users in the apt_no where wastage was detected
    users = mod_func.retrieve_users(apt_no)
    if users is False:
        print "No users that are active"
        return
    # Create notification for active users
    for user in users:
        reg_id = user.reg_id
        notif_id = random.choice(string.digits)

        # Construct the message
        message_to_send = {}
        message_to_send['msg_type'] = 'response'
        message_to_send['api'] = ENERGY_WASTAGE_NOTIF_API
        message_to_send['options'] = {}
        message_to_send['options']['id'] = notif_id
        message_to_send['options'][
            'message'] = ('Please turn off the Light in the Bedroom' + str(notif_id)) * 3

        message = create_message(reg_id, message_to_send)

        # Send the message to all the users
        client.send_message(message)

        print "Sending wastage notification for:: " + user.name


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
