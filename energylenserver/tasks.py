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
import math
import random
import string
from types import NoneType
from datetime import timedelta

import pandas as pd
import numpy as np
import datetime as dt
from multiprocessing.managers import BaseManager

from celery import shared_task
from django_pandas.io import read_frame

"""
Imports from EnergyLens+
"""
# DB Imports
from energylenserver.models.models import *
from energylenserver.models.DataModels import *
from energylenserver.models import functions as mod_func

# Core Algo imports
from energylenserver.core import classifier
from energylenserver.core.constants import wastage_threshold, upload_interval, no_test_data
from energylenserver.core import functions as core_f
from energylenserver.meter import edge_detection
from energylenserver.core import user_attribution as attrib
from energylenserver.core import apportionment as apprt
from energylenserver.meter import edge_matching as e_match
from energylenserver.core.functions import exists_in_metadata

# GCM Client imports
from energylenserver.gcmxmppclient.messages import create_message

# Reporting API imports
from energylenserver.api import reporting as rpt

# Common imports
from energylenserver.common_imports import *
from energylenserver.constants import apt_no_list

# Enable Logging
upload_logger = logging.getLogger('energylensplus_upload')
logger = logging.getLogger('energylensplus_django')
elogger = logging.getLogger('energylensplus_error')
meter_logger = logging.getLogger('energylensplus_meterdata')


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

# '''
# Establishing connection with the running GCM Server
try:
    ClientManager.register('get_client_obj')
    manager = ClientManager(address=('localhost', 50000), authkey='abracadabra')
    manager.connect()
    client = manager.get_client_obj()
    if client is None or client == "":
        logger.debug("GCM Client not connected")
    else:
        logger.debug("Got the GCM Client: client obj type:: %s", type(client))
except Exception, e:
    elogger.error("[InternalGCMClientConnectionException] %s", e)
# '''

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

    try:
        # Create a dataframe for preprocessing
        if sensor_name != 'rawaudio':
            try:
                df_csv = pd.read_csv(filepath)
            except Exception, e:
                logger.error("[InsertDataException]::%s", str(e))
                os.remove(filepath)
                return

        # Remove rows with 'Infinity' in MFCCs created
        if sensor_name == 'audio':
            if str(df_csv.mfcc1.dtype) != 'float64':
                df_csv = df_csv[df_csv.mfcc1 != '-Infinity']

        if sensor_name != 'rawaudio':
            # logger.debug("Total number of records to insert: %d", len(df_csv))

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

        logger.debug("FILE:: %s", filename)
        '''
        if sensor_name != 'rawaudio':
            logger.debug("FILE:: %s", filename)
            if len(df_csv) > 0:
                logger.debug("%s[%s] -- [%s]", sensor_name,
                             time.ctime(df_csv.ix[df_csv.index[0]]['time'] / 1000),
                             time.ctime(df_csv.ix[df_csv.index[-1]]['time'] / 1000))
            else:
                logger.debug("%s empty!!", filename)
        else:
            logger.debug("FILE:: %s", filename)
        '''
        # Classify location
        if sensor_name == 'wifi' and training_status is False:
            logger.debug("Classifying new data for: %s with filename: %s", sensor_name, filename)
            start_time = df_csv.ix[df_csv.index[0]]['time']
            end_time = df_csv.ix[df_csv.index[-1]]['time']

            location = classifier.localize_new_data(user.apt_no, start_time, end_time, user)
            logger.debug("%s is in %s", user.name, location)

        upload_logger.debug("Successful Upload! File: %s", filename)
    except Exception, e:
        logger.exception("[PhoneDataHandlerException]:: %s", e)


@shared_task
def meterDataHandler(df, file_path):
    """
    Consumes sensor data streams from the meter
    """

    meter_uuid_folder = os.path.dirname(file_path)
    uuid = meter_uuid_folder.split('/')[-1]

    # Start the process only if participants are registered
    try:
        meter = MeterInfo.objects.get(meter_uuid=uuid)
    except MeterInfo.DoesNotExist, e:
        meter_logger.debug("No registered users for this apt meter")
        return

    apt_no = meter.apt_no
    meter_logger.debug("Detecting Edges for Apt:: %s UUID:: %s", apt_no, uuid)

    try:
        # -- Detect Edge --
        edges_df = edge_detection.detect_and_filter_edges(df)
    except Exception, e:
        meter_logger.exception("[OuterDetectEdgeException]:: %s", str(e))

    # -- Store edges into db --

    if len(edges_df) == 0:
        meter_logger.debug("No edges detected")
        return

    # For the detected edge, store edge and call classification pipeline task
    for idx in edges_df.index:
        edge = edges_df.ix[idx]
        edge_time = edge.time
        magnitude = edge.magnitude

        try:

            # Edge Filter: Forward edge only it exists in the metadata
            data = mod_func.retrieve_metadata(apt_no)
            metadata_df = read_frame(data, verbose=False)
            in_metadata, matched_md = exists_in_metadata(apt_no, "all", "all",
                                                         math.fabs(magnitude),
                                                         metadata_df,
                                                         meter_logger, "dummy_user")
            if not in_metadata:
                meter_logger.debug("Detected edge of magnitude %d ignored", magnitude)
                continue

            # Check if the edge exists in the database
            try:
                # Edge Filter: filter periodic edges of similar mag
                # Cause: fridge or washing machine
                obj = Edges.objects.filter(meter=meter).latest('timestamp')
                prev_time = int(obj.timestamp)
                prev_mag = math.fabs(obj.magnitude)
                diff = prev_mag / math.fabs(magnitude)
                if (diff > 0.8 and diff <= 1) and (edge_time - prev_time < 60 and
                                                   math.fabs(magnitude) < 600):
                    continue
                record = Edges.objects.get(meter=meter, timestamp=edge_time)
            except Edges.DoesNotExist, e:

                # --Store edge--
                edge_r = Edges(timestamp=int(edge_time), time=dt.datetime.fromtimestamp(edge_time),
                               magnitude=magnitude, type=edge.type,
                               curr_power=edge.curr_power, meter=meter)
                edge_r.save()

                meter_logger.debug("Edge for UUID: %s at [%s] of mag %d", uuid, time.ctime(
                    edge['time']), edge['magnitude'])

                # Initiate classification pipeline
                edgeHandler(edge_r)
        except Exception, e:
            meter_logger.error("[EdgeSaveException]:: %s", str(e))


@shared_task
def edgeHandler(edge):
    """
    Starts the classification pipeline and relays edges based on edge type
    """
    logger.debug("Starting the Classification pipeline..")
    if edge.type == "falling":
        chain = (classify_edge.s(edge) |
                 find_time_slice.s() | apportion_energy.s())
    else:
        chain = classify_edge.s(edge)
    chain()
    logger.debug("Classification Pipeline ended for edge: [%s] :: %d",
                 time.ctime(edge.timestamp), edge.magnitude)

"""
Invokes the EnergyLens+ core algorithm
"""


@shared_task
def classify_edge(edge):
    """
    Consumes smart meter edges and phone data to give out 'who',
    'what', 'where' and 'when'
    :param edge:
    :return "where", what" and "who" labels:
    """

    try:
        return_error = 'ignore', 'ignore', 'ignore', 'ignore'

        apt_no = edge.meter.apt_no
        logger.debug("Apt.No.:: %d Classify edge type '%s' [%s] %d",
                     apt_no, edge.type, time.ctime(edge.timestamp), edge.magnitude)

        # Defining event window
        p_window = 60 * 2  # window for each side of the event time (in seconds)

        event_time = edge.timestamp
        magnitude = edge.magnitude

        if edge.type == "rising":
            event_type = "ON"
            start_time = event_time - 60
            end_time = event_time + p_window
        else:
            event_type = "OFF"
            start_time = event_time - p_window
            end_time = event_time

            now_time = int(time.time())
            if (now_time - event_time) <= 2 * 60:
                edgeHandler.apply_async(args=[edge], countdown=upload_interval)
                return return_error

        # --- Preprocessing ---
        # Step 2: Determine user at home
        user_list = core_f.determine_user_home_status(start_time, end_time, apt_no)
        n_users_at_home = len(user_list)

        if n_users_at_home == 0:
            logger.debug("No user at home. Ignoring edge activity.")
            return return_error

        # --- Classification --
        location_dict = {}
        appliance_dict = {}

        # Get user details
        users = mod_func.get_users(user_list)
        logger.debug("Users at home: %s", users)
        for user in users:
            dev_id = user.dev_id

            logger.debug("For User: %s Mag: %s", user, magnitude)

            # Step 1: Determine location for every user
            location = classifier.classify_location(
                apt_no, start_time, end_time, user, edge, n_users_at_home)
            if isinstance(location, bool):
                continue
            elif location == no_test_data:

                now_time = int(time.time())
                if (now_time - event_time) < 15 * 60:
                    edgeHandler.apply_async(args=[edge], countdown=upload_interval)
                    return return_error
                else:
                    continue
            else:
                location_dict[dev_id] = location

            # Step 2: Determine appliance for every user using audio
            appliance = classifier.classify_appliance(
                apt_no, start_time, end_time, user, edge, n_users_at_home)
            if isinstance(appliance, bool):
                continue
            elif appliance == no_test_data:

                now_time = int(time.time())
                if (now_time - event_time) < 15 * 60:
                    edgeHandler.apply_async(args=[edge], countdown=upload_interval)
                    return return_error
                else:
                    continue
            else:
                appliance_dict[dev_id] = appliance

        logger.debug("Determined Locations: %s", location_dict)
        logger.debug("Determined Appliances: %s", appliance_dict)

        if len(location_dict) == 0 or len(appliance_dict) == 0:
            return return_error
        elif False in location_dict or False in appliance_dict:
            return return_error

        # Step 3: Determine user based on location, appliance and metadata
        if n_users_at_home > 1:
            user = attrib.identify_user(
                apt_no, magnitude, location_dict, appliance_dict, user_list, edge)
            who = user['dev_id']
            where = user['location']
            what = user['appliance']
            logger.debug("Determined user: %s", who)
            if isinstance(who, list):
                user_records = []
                for u in who:
                    user_records.append(mod_func.get_user(u))
                who = user_records

        elif n_users_at_home == 1:
            user_record = users.first()
            user = user_record.dev_id
            who = [user_record]
            where = location_dict[user]
            what = appliance_dict[user]

        logger.debug("[%s] - [%d] :: Determined labels: %s %s %s" %
                     (time.ctime(event_time), magnitude, who, where, what))

        # ---FILTER start---
        # Save only if the number of existing ongoing events for determined appliance
        # in the inferred location with the specified magnitude does not exceed
        # the number of appliances

        if where != "Unknown":

            # Get count of inferred appliance in the inferred location
            data = mod_func.retrieve_metadata(apt_no)
            metadata_df = read_frame(data, verbose=False)
            metadata_df['appliance'] = metadata_df.appliance.apply(lambda s: s.split('_')[0])

            # Determine if presence based
            md_df = metadata_df.ix[:, ['appliance', 'presence_based']].drop_duplicates()
            md_df.reset_index(inplace=True, drop=True)

            if not md_df.ix[0]['presence_based']:
                metadata_df = metadata_df[metadata_df.appliance == what]
            else:
                metadata_df = metadata_df[(metadata_df.location == where) &
                                          (metadata_df.appliance == what)]
            metadata_df.reset_index(inplace=True, drop=True)

            if len(metadata_df) == 0:
                # Inference is incorrect
                # Causes:
                #  1. Location or appliance was incorrect
                #  2. Only a single occupant present in the home, and:
                        # User didn't carry his phone with him
                        # A similar spurious event was detected

                who = "Unknown"
                where = "Unknown"
                what = "Unknown"

            else:
                # Determine the on going events of inferred appliance in the inferred location
                on_event_records = mod_func.get_on_events_by_location(apt_no, end_time, where)
                on_event_records_df = read_frame(on_event_records, verbose=False)
                on_event_records_df = on_event_records_df[on_event_records_df.appliance == what]
                n_on_event_records = len(on_event_records_df)
                logger.debug("Number of ongoing events: %s", n_on_event_records)

                if n_on_event_records > 0 and event_type == 'ON':

                    no_of_appl = metadata_df.ix[0]['how_many']
                    logger.debug("Count for %s in %s: %d", what, where, no_of_appl)

                    if n_on_event_records >= no_of_appl:
                        who = "Unknown"
                        where = "Unknown"
                        what = "Unknown"
                elif n_on_event_records == 0 and event_type == 'OFF':
                    # Falling edge with no ON events
                    who = "Unknown"
                    where = "Unknown"
                    what = "Unknown"
            logger.debug("[%s] - [%d] :: After filter: Determined labels: %s %s %s" %
                         (time.ctime(event_time), magnitude, who, where, what))

        # --- FILTER end---

        if isinstance(who, list):

            # New record for each user for an edge - indicates multiple occupants
            # were present in the room during the event
            for user in who:
                # Create a record in the Event Log with edge id
                # and store 'who', 'what', 'where' labels
                event = EventLog(edge=edge, event_time=event_time,
                                 location=where, appliance=what, dev_id=user,
                                 event_type=event_type, apt_no=apt_no)
                event.save()

                # ONLY FOR TESTING
                message = "In %s, %s uses %s consuming %s Watts" % (
                    where, user.name, what, magnitude)
                inform_user(user.dev_id, message)
                logger.debug("Notified User: %s", who)

        # For "Unknown" label
        elif isinstance(who, str):
            user_record = mod_func.get_user(unknown_id)
            # Create a record in the Event Log with edge id
            # and store 'who', 'what', 'where' labels
            event = EventLog(edge=edge, event_time=event_time,
                             location=where, appliance=what, dev_id=user_record,
                             event_type=event_type, apt_no=apt_no)
            event.save()

    except Exception, e:
        logger.exception("[ClassifyEdgeException]:: %s", e)
        return 'ignore', 'ignore', 'ignore', 'ignore'

    return who[0], what, where, event


@shared_task
def find_time_slice(result_labels):
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
    return_error = 'ignore', 'ignore', 'ignore', 'ignore'
    try:
        who, what, where, off_event = result_labels

        # If no user at home or no identified users, skip off_event
        if (who == 'ignore' and what == 'ignore' and
            where == 'ignore') or (what == 'Unknown' and
                                   where == 'Unknown'):
            return return_error

        apt_no = off_event.edge.meter.apt_no
        off_time = off_event.event_time
        off_mag = off_event.edge.magnitude

        logger.debug("Determining activity duration: [%s] :: %s" % (
            time.ctime(off_time), str(off_mag)))

        # Match ON/OFF events
        matched_on_event = e_match.match_events(apt_no, off_event)

        if isinstance(matched_on_event, bool):
            logger.debug("No ON event found")
            return return_error

        # Mark ON event as matched
        matched_on_event.matched = True
        matched_on_event.save()

        off_event.matched = True
        off_event.save()

        # Inferred activity time slice
        start_time = matched_on_event.event_time
        end_time = off_time

        power = round((math.fabs(off_mag) + matched_on_event.edge.magnitude) / 2)
        usage = apprt.get_energy_consumption(start_time, end_time, power)

        # Save Activity
        activity = ActivityLog(start_time=start_time, end_time=end_time,
                               location=where, appliance=what,
                               power=power, usage=usage,
                               meter=off_event.edge.meter,
                               start_event=matched_on_event,
                               end_event=off_event)
        activity.save()

        logger.debug("[%d] Time slice for activity: %s uses %s in %s between %s and %s",
                     apt_no, who, what, where, time.ctime(start_time), time.ctime(end_time))
    except Exception, e:
        logger.exception("[FindTimeSliceException]:: %s", str(e))
        return return_error

    return apt_no, start_time, end_time, activity

"""
Invokes the components that use EnergyLens+ outputs - who, what, where and when:
1. Wastage Detection
2. Energy Apportionment
"""


@shared_task
def apportion_energy(result_labels):
    """
    Determines the energy usage/wastage of an individual based on
    activity parameters and length of stay for each activity

    :return: energy usage
    """
    try:
        apt_no, start_time, end_time, activity = result_labels

        if (start_time == 'ignore' and end_time == 'ignore' and
                activity == 'ignore'):
            logger.debug("Ignoring apportionment request")
            return

        # Activity labels
        act_loc = activity.location
        act_appl = activity.appliance

        logger.debug("[%d] Apportioning energy:: Using %s in %s between %s and %s",
                     apt_no, act_appl, act_loc, time.ctime(start_time), time.ctime(end_time))

        user_list = core_f.determine_user_home_status(start_time, end_time, apt_no)
        n_users_at_home = len(user_list)

        if n_users_at_home == 0:
            logger.error("No user at home. Something went wrong!")
            return

        # Determine appliance type
        md_records = mod_func.retrieve_metadata(apt_no)
        metadata_df = read_frame(md_records, verbose=False)
        metadata_df['appliance'] = metadata_df.appliance.apply(lambda s: s.split('_')[0])
        metadata_df = metadata_df[metadata_df.appliance == act_appl]
        metadata_df = metadata_df.ix[:, ['appliance', 'presence_based']].drop_duplicates()
        metadata_df.reset_index(drop=True, inplace=True)

        md_entry = metadata_df.ix[0]

        # For non-presence based appliances e.g Geyser, Microwave, Music System, Fridge
        if not md_entry.presence_based:
            power = activity.power
            usage = apprt.get_energy_consumption(start_time, end_time, power)
            stayed_for = end_time - start_time
            user = activity.start_event.dev_id
            usage_entry = EnergyUsageLog(activity=activity,
                                         start_time=start_time, end_time=end_time,
                                         stayed_for=stayed_for, usage=usage,
                                         dev_id=user, shared=False)
            usage_entry.save()
            return

        # For presence based appliances, apportion based on stay duration

        presence_df = pd.DataFrame(columns=['start_time', 'end_time'])
        users_list = [mod_func.get_user(u) for u in user_list]
        for user in users_list:

            user_id = user.dev_id

            # Determine duration of stay in the room for a user
            df = core_f.get_presence_matrix(
                apt_no, user, start_time, end_time, act_loc)

            if isinstance(df, NoneType) or len(df) == 0:
                continue
            presence_df[str(user_id)] = df['label']
            presence_df['start_time'] = df['start_time']
            presence_df['end_time'] = df['end_time']

        if isinstance(presence_df, NoneType) or len(presence_df) == 0:
            logger.debug("Empty presence matrix formed")
            return
        # Merge slices where the user columns have the same values
        presence_df = core_f.merge_presence_matrix(presence_df)
        logger.debug("Presence matrix::\n %s", presence_df)

        # Determine actual usage/wastage of a user based on
        # time of stay in the room of activity handling all complex cases
        apprt.calculate_consumption(users_list, presence_df, activity)

    except Exception, e:
        logger.exception("[ApportionEnergyException]:: %s", str(e))


@shared_task
def determine_wastage(apt_no):
    """
    Determines energy wastage in real-time
    """
    logger.debug("Periodic Energy Wastage Detector started for [%s]", apt_no)
    try:
        now_time = int(time.time())
        end_time = now_time - upload_interval
        start_time = end_time - wastage_threshold

        # Determine the on going events
        on_event_records = mod_func.get_on_events_by_location(apt_no, end_time, "all")
        n_on_event_records = on_event_records.count()

        if n_on_event_records == 0:
            return

        on_events = []

        # Retrieve last 30 minutes' events
        for event in on_event_records:
            on_time = event.event_time
            if (now_time - on_time) <= 30 * 60:
                on_events.append(event)

        logger.debug("Number of ongoing events: %s", len(on_events))

        if len(on_events) == 0:
            return

        # Determine users at home
        user_list = core_f.determine_user_home_status(start_time, end_time, apt_no)
        n_users_at_home = len(user_list)

        if n_users_at_home == 0:
            logger.debug("No users at home")
            return

        # Build presence matrix for the ongoing activities
        users = mod_func.get_users(user_list)

        pres_loc = {}
        for user in users:
            pres_loc[user.dev_id] = []

        presence_df = pd.DataFrame(columns=['start_time', 'end_time'])
        for event in on_events:

            what = event.appliance
            where = event.location

            logger.debug("Event: [%s - %s]", what, where)

            if what == "Unknown":
                continue

            # Go ahead only if it is a presence based appliance
            md_records = mod_func.retrieve_metadata(apt_no)
            metadata_df = read_frame(md_records, verbose=False)
            metadata_df['appliance'] = metadata_df.appliance.apply(lambda s: s.split('_')[0])
            metadata_df = metadata_df[metadata_df.appliance == what]
            metadata_df = metadata_df.ix[:, ['appliance', 'presence_based']].drop_duplicates()
            metadata_df.reset_index(drop=True, inplace=True)

            md_entry = metadata_df.ix[0]

            if not md_entry.presence_based:
                continue

            for user in users:
                user_id = user.dev_id

                if len(pres_loc[user_id]) > 0 and where in pres_loc[user_id]:
                    continue

                # Build presence matrix
                df = core_f.get_presence_matrix(apt_no, user, start_time, end_time, where)

                if isinstance(df, NoneType) or len(df) == 0:
                    continue
                presence_df[str(user_id)] = df['label']
                presence_df['start_time'] = df['start_time']
                presence_df['end_time'] = df['end_time']
                pres_loc[user_id].append(where)

            if isinstance(presence_df, NoneType) or len(presence_df) == 0:
                logger.debug("Empty matrix formed")
                return

            # Merge slices where the user columns have the same values
            # logger.debug("Presence DF: \n %s", presence_df)
            presence_df = core_f.merge_presence_matrix(presence_df)
            # logger.debug("Merged Presence Matrix:\n %s", presence_df)

            # Determine wastage - find rooms of activity that are empty
            user_columns = presence_df.columns - ['start_time', 'end_time']
            last_idx = presence_df.index[-1]
            col_sum = presence_df.ix[last_idx, user_columns].sum(axis=1, numeric_only=True)
            # w_slices_ix = presence_df.index[np.where(col_sum == 0)[0]]

            # Save and send notifications to all the users
            if int(col_sum) == 0:
                message = "Energy wastage detected in %s! %s is left ON." % (
                    where, what)
                # Save
                for user in users:
                    wastage = EnergyWastageNotif(dev_id=user, time=time.time(),
                                                 appliance=what,
                                                 location=where,
                                                 message=message)
                    wastage.save()

                # Inform
                inform_all_users(apt_no, message, users)

    except Exception, e:
        logger.exception("[DetermineWastageRTException]:: %s", e)


@shared_task(name='tasks.realtime_wastage_notif')
def realtime_wastage_notif():
    """
    Determines energy wastage in real-time and sends notification to all the
    users
    """
    try:
        # Determine wastage for each apt no independently
        for apt_no in apt_no_list:
            if apt_no in [102, '102A']:
                apt_no = 102
            determine_wastage.delay(apt_no)

    except Exception, e:
        logger.exception("DetermineWastageException]:: %s", str(e))


"""
Invokes the real-time feedback component
"""


@shared_task(name='tasks.send_validation_report')
def send_validation_report():
    """
    Sends a ground truth validation report to all the users
    """
    logger.debug("Creating periodic validation report..")
    try:
        # Get all the users
        users = mod_func.get_all_active_users()
        if users is False:
            logger.debug("No users are active")
            return
        for user in users:
            reg_id = user.reg_id
            apt_no = user.apt_no

            # Construct the message
            data_to_send = {}
            data_to_send['msg_type'] = "response"
            data_to_send['api'] = GROUND_TRUTH_NOTIF_API
            data_to_send['options'] = {}
            activities = rpt.get_inferred_activities(user)

            if isinstance(activities, bool):
                return

            if len(activities) == 0:
                return

            appliances = []
            records = mod_func.retrieve_metadata(apt_no)
            if records:
                metadata_df = read_frame(records, verbose=False)
                metadata_df['appliance'] = metadata_df.appliance.apply(lambda s: s.split('_')[0])
                metadata_df = metadata_df.ix[:, ['location', 'appliance']].drop_duplicates()
                metadata_df.reset_index(drop=True, inplace=True)
                for idx in metadata_df.index:
                    entry = metadata_df.ix[idx]
                    appliances.append({'location': entry.location, 'appliance': entry.appliance})
            appliances.append({'location': "Unknown", 'appliance': "Unknown"})
            '''
            appliances.append({'location': "Bedroom", 'appliance': "TV"})
            appliances.append({'location': "Dining Room", 'appliance': "TV"})
            appliances.append({'location': "Study", 'appliance': "Computer"})
            '''

            users = mod_func.retrieve_users(apt_no)

            occupants = {}
            for user_i in users:
                occupants[user_i.dev_id] = user_i.name

            data_to_send['options']['activities'] = activities
            data_to_send['options']['appliances'] = appliances
            data_to_send['options']['occupants'] = occupants

            # Send the message
            send_notification(reg_id, data_to_send)
            logger.debug("Sent report to:: %s", user.name)
    except Exception, e:
        logger.exception("[SendingReportException]:: %s", e)


@shared_task
def inform_all_users(apt_no, notif_message, users):
    """
    Informs the user by sending a notification to all the users
    :return message:
    """

    import random
    import string

    try:
        # Create notification for active users
        for user in users:
            reg_id = user.reg_id
            notif_id = random.choice(string.digits)

            # notif_message = ('Please turn off the Light in the Bedroom' + str(notif_id)) * 3
            # Construct the message
            message_to_send = {}
            message_to_send['msg_type'] = 'response'
            message_to_send['api'] = ENERGY_WASTAGE_NOTIF_API
            message_to_send['options'] = {}
            message_to_send['options']['id'] = notif_id
            message_to_send['options']['message'] = notif_message

            # Send the message
            time_to_live = 30 * 60
            send_notification(reg_id, message_to_send, time_to_live)
            logger.debug("Notified user :: %s", user.name)
    except Exception, e:
        logger.error("[InformAllUsersException]:: %s", e)


@shared_task
def inform_user(dev_id, notif_message):
    """
    Sends a notification to a specific user
    """
    try:

        # Get user
        user = mod_func.get_user(dev_id)
        if user is False:
            logger.error("Specified user does not exist")
            return

        reg_id = user.reg_id
        notif_id = random.choice(string.digits)

        # Construct the message
        message_to_send = {}
        message_to_send['msg_type'] = 'response'
        message_to_send['api'] = ENERGY_WASTAGE_NOTIF_API
        message_to_send['options'] = {}
        message_to_send['options']['id'] = notif_id
        message_to_send['options']['message'] = notif_message

        # Send the message
        send_notification(reg_id, message_to_send)
        logger.debug("Notified user :: %s", user.name)
    except Exception, e:
        logger.exception("[InformUserException]:: %s", e)


@shared_task
def send_notification(reg_id, message_to_send, time_to_live=3600):
    """
    Sends a message to a specific user
    """
    try:
        message = create_message(reg_id, message_to_send, time_to_live)
        client.send_message(message)
    except Exception, e:
        logger.exception("[SendingNotifException]:: %s", e)

"""
Test Task
"""


@shared_task
def send_msg(reg_id):
    logger.debug("Sending Message at [%s]", time.ctime(time.time()))
    message_to_send = {}
    message_to_send['msg_type'] = 'response'
    message_to_send['api'] = ENERGY_WASTAGE_NOTIF_API
    message_to_send['options'] = {}
    message_to_send['options']['message'] = 'Please turn off the Light in the Bedroom'
    message = create_message(reg_id, message_to_send)
    client.send_message(message)
    logger.debug("Message Sent at [%s]", time.ctime(time.time()))


'''
Test Tasks
@shared_task
def test_task(x):
    logger.debug("X=" + str(x)
    relay(x)


def relay(x):
    logger.debug("Called relay"
    chain = add.s(x, 10) | mul.s(1001)
    chain()

@shared_task
def add(x,y):
    logger.debug("Called Add"
    logger.debug("x="+ str(x) + " y=" + str(y)
    z = x+y
    return x,y,z

@shared_task
def mul(s, n):
    logger.debug("Called mul"
    print n
    print s
    (x,y,z) = s
    logger.debug("x="+ str(x) + " y=" + str(y) + " z=" + str(z)
    w = x * y * z
    logger.debug("W=" + w
'''
