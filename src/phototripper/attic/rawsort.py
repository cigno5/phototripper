from functools import reduce

import googlemaps
from datetime import datetime
import argparse
import logging
import json
import os
import re
import subprocess
import requests
from datetime import datetime, timedelta
from math import radians, sin, cos, sqrt, asin

from geopy.geocoders.base import Geocoder
from unidecode import unidecode

global GOOGLE_API_KEY

from geopy.geocoders import Nominatim, Photon
import json

IMAGE_EXTS = ["cr2", "cr3", "jpg", "3fr", "raf", "arw"]


# landmark
# historical_landmark
# tourist_attraction
# locality
# natural_feature
# point_of_interest
# national_park

class GeoDecoder:
    geolocators = [
        Nominatim(user_agent="exif_reader"),
        Photon(user_agent="exif_reader")
    ]

    suppliers = ['google', 'geopy']

    def __init__(self, supplier='google'):
        self.supplier = supplier
        self.cache = {}

    def get_cached_place(self, latlon):
        k = (round(latlon[0], 4), round(latlon[1], 4))
        if k not in self.cache:
            self.cache[k] = self.get_place(latlon)

        return self.cache[k]

    def get_place(self, latlon):
        logging.debug(f"Retrieving place for {latlon} with {self.supplier}")
        place = None
        if self.supplier == 'geopy':
            place = self.get_place_geopy(latlon)

        if self.supplier == 'google':
            self.get_google_place(latlon)

        if self.supplier == 'google2':
            self.get_categorized_location_name_hybrid(latlon)

        if self.supplier == 'google3':
            # self.get_categorized_location_name_final(latlon)
            self.get_gmaps_lib(latlon)

        if place:
            place = unidecode(place)

        return place

    @staticmethod
    def haversine(latlon1, latlon2):
        R = 6371000  # meters
        dlat = radians(latlon2[0] - latlon1[0])
        dlon = radians(latlon2[1] - latlon1[1])

        a = sin(dlat / 2) ** 2 + cos(radians(latlon1[0])) * cos(radians(latlon2[0])) * sin(dlon / 2) ** 2
        return 2 * R * asin(sqrt(a))

    def get_google_place(self, latlon):
        geo = self.reverse_geocode_gmap(latlon)
        comps = geo["results"]
        logging.debug(f"Raw decode: {json.dumps(geo, indent=2)}")

        place = None

        def find(types):
            for _comp in comps:
                comp = _comp["address_components"]
                for c in comp:
                    if any(t in c["types"] for t in types):
                        return c["long_name"]
            return None

        # comp = geo["results"][0]["address_components"]
        # def find(types):
        #     for c in comp:
        #         if any(t in c["types"] for t in types):
        #             return c["long_name"]
        #     return None

        # 1) City
        city, city_dist = GeoDecoder.get_nearest_city_with_distance(latlon)

        # If truly inside the city → collapse
        if city and city_dist < 3000:
            place = find([
                "locality",
                "postal_town",
                "town",
                "village"
            ])
            logging.debug(f"  City: {place}")

        # 2) POI
        if not place:
            place = self.get_poi_gmaps(latlon)
            logging.debug(f"  POI: {place}")

        # 3) Named area
        if not place:
            place = find([
                "neighborhood",
                "sublocality",
                "administrative_area_level_3",
                "administrative_area_level_2"
            ])
            logging.debug(f"  Named area: {place}")

        # 4) Region
        if not place:
            place = find(["administrative_area_level_1"])
            logging.debug(f"  Region: {place}")

        # 5) Country
        if not place:
            place = find(["country"])
            logging.debug(f"  Country: {place}")

        return place

    def get_place_geopy(self, latlon):
        _place = None
        for geolocator in self.geolocators:
            location = geolocator.reverse(latlon, language="en")
            if location:
                addr = location.raw.get("address", {})
                _place = (addr.get("tourism") or
                          addr.get("city") or
                          addr.get("town") or
                          addr.get("village") or
                          addr.get("hamlet") or
                          addr.get("suburb") or
                          addr.get("locality") or
                          addr.get("amenity") or
                          addr.get("neighbourhood") or
                          addr.get("road") or
                          addr.get("county"))
                logging.debug(f"Location: {location}")
                logging.debug(f"Raw data: {json.dumps(location.raw, indent=2)}")
                logging.debug(f"Raw data: {json.dumps(location.raw.get("address", {}), indent=2)}")
                logging.debug(f"Place   : {_place}")

        return _place

    def get_poi_gmaps(self, latlon, radius=300):
        """
        Use Google Places Nearby Search to get a photographer-friendly place name.
        Radius in meters.
        """
        url = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"

        params = {
            "location": f"{latlon[0]},{latlon[1]}",
            "radius": radius,
            # Prioritise places photographers care about
            "type": ["tourist_attraction", "natural_feature"],
            "key": GOOGLE_API_KEY,
            "language": "en"
        }

        r = requests.get(url, params=params)
        data = r.json()

        if data.get("status") not in ("OK", "ZERO_RESULTS"):
            raise RuntimeError(f"Google Places error: {data.get('status')}")

        if not data.get("results"):
            return None

        # Results are sorted by prominence by default
        place = data["results"][0]

        return place["name"]

    def get_city_gmap(self, latlon):
        url = "https://maps.googleapis.com/maps/api/geocode/json"
        params = {
            "latlng": f"{latlon[0]},{latlon[1]}",
            "key": GOOGLE_API_KEY,
            "language": "en"
        }

        r = requests.get(url, params=params).json()
        if r["status"] != "OK":
            return None

        for comp in r["results"][0]["address_components"]:
            if "locality" in comp["types"]:
                return comp["long_name"]

        return None

    def reverse_geocode_gmap(self, latlon):
        """
        Call Google Reverse Geocoding API and return the raw JSON response.
        """
        url = "https://maps.googleapis.com/maps/api/geocode/json"

        params = {
            "latlng": f"{latlon[0]},{latlon[1]}",
            "key": GOOGLE_API_KEY,
            "language": "en"
        }

        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()

        data = response.json()

        if data.get("status") != "OK":
            raise RuntimeError(
                f"Google Geocoding error: {data.get('status')} "
                f"{data.get('error_message', '')}"
            )

        return data

    @staticmethod
    def get_nearest_city_with_distance(latlon):
        url = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
        params = {
            "location": f"{latlon[0]},{latlon[1]}",
            "rankby": "distance",
            "type": "locality",
            "key": GOOGLE_API_KEY,
            "language": "en"
        }

        r = requests.get(url, params=params).json()
        if r.get("status") != "OK" or not r.get("results"):
            return None, None

        city = r["results"][0]
        city_lat = city["geometry"]["location"]["lat"]
        city_lon = city["geometry"]["location"]["lng"]

        dist = GeoDecoder.haversine(latlon, (city_lat, city_lon))
        return city["name"], dist

    @staticmethod
    def get_categorized_location_name(coordinates: tuple) -> str:
        """
        Retrieves a location name for folder categorization based on custom
        granularity rules using the Google Geocoding API.

        Args:
            coordinates: A tuple of (latitude, longitude).
            api_key: Your Google Maps Platform API Key.

        Returns:
            A string representing the most appropriate location name
            (e.g., "Eiffel Tower", "New York City", "Aveyron").
        """
        lat, lng = coordinates
        endpoint = "https://maps.googleapis.com/maps/api/geocode/json"

        # 1. Reverse Geocoding API Call
        params = {
            'latlng': f"{lat},{lng}",
            'key': GOOGLE_API_KEY,
        }

        try:
            response = requests.get(endpoint, params=params)
            response.raise_for_status()  # Raise an exception for HTTP errors (4xx or 5xx)
            data = response.json()
        except requests.exceptions.RequestException as e:
            print(f"Error during API request: {e}")
            return "Unknown_Location_API_Error"

        if data['status'] != 'OK' or not data['results']:
            return "Unknown_Location_No_Data"

        # The 'results' array contains one or more geocoded addresses for the location.
        # The first result is generally the most specific (e.g., a street address or landmark).

        # 2. Applying Custom Granularity Logic

        # --- A. Check for Notable Landmark/Specific Tourist Attraction (Highest Priority) ---
        # We examine the 'types' field of the most specific result.
        most_specific_result = data['results'][0]

        # Common types for landmarks/attractions: 'point_of_interest', 'establishment', 'park', 'museum', etc.
        # The presence of the 'point_of_interest' type and a specific name is a good indicator.

        if 'point_of_interest' in most_specific_result['types'] and 'establishment' in most_specific_result['types']:
            # This often correctly captures a specific building, statue, or attraction.
            return most_specific_result['formatted_address'].split(',')[
                0].strip()  # Takes the first, most descriptive part

        # --- B. Check for City/Locality (Next Priority) ---
        # Loop through the results to find a component that is a city/locality.
        for result in data['results']:
            # This checks for a clear 'locality' or 'postal_town' (which often mean 'city').
            if any(t in result['types'] for t in ['locality', 'postal_town']):
                # Extract the city name from the address components
                for component in result['address_components']:
                    if 'locality' in component['types'] or 'postal_town' in component['types']:
                        return component['long_name']

        # --- C. Middle of Nowhere / Region / Administrative Area (Lowest Priority) ---
        # If no specific landmark or city is found, we escalate to the next highest administrative level.
        for result in data['results']:
            # Check for administrative areas (e.g., state, region, county)
            if any(t in result['types'] for t in ['administrative_area_level_1', 'administrative_area_level_2']):
                # Extract the area name
                for component in result['address_components']:
                    if 'administrative_area_level_1' in component['types'] or 'administrative_area_level_2' in \
                            component['types']:
                        # Use the first administrative area found (often the state or province)
                        return component['long_name']

        # --- D. Fallback ---
        # If none of the above criteria are met, use the country or a generic tag.
        # We find the country and use that.
        for result in data['results']:
            if 'country' in result['types']:
                for component in result['address_components']:
                    if 'country' in component['types']:
                        return component['long_name']

        # Final Catch-all
        return "Unclassified_Location"

    @staticmethod
    def get_categorized_location_name_hybrid(coordinates: tuple) -> str:
        """
        Retrieves a location name by prioritizing specific landmarks (Places API v1)
        and falling back to administrative areas (Geocoding API).

        Args:
            coordinates: A tuple of (latitude, longitude).

        Returns:
            A string representing the most appropriate location name.
        """
        lat, lng = coordinates

        # -------------------------------------------------------------
        # 1. PRIORITY 1: SEARCH NEARBY LANDMARKS (Places API v1 - New)
        #    Target: Tourist attractions, natural features (e.g., Vestrahorn, Eiffel Tower)
        # -------------------------------------------------------------

        places_endpoint = "https://places.googleapis.com/v1/places:searchNearby"

        # Use a small radius (e.g., 2 km) to ensure specificity
        radius_meters = 2000

        places_headers = {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": GOOGLE_API_KEY,
            # Mandatory FieldMask: Only request name and type to control costs
            "X-Goog-FieldMask": "places.displayName,places.types,places.primaryType"
        }

        places_payload = {
            "locationRestriction": {
                "circle": {
                    "center": {"latitude": lat, "longitude": lng},
                    "radius": radius_meters
                }
            },
            # **UPDATED: Using supported types for natural features and attractions**
            "includedTypes": ["tourist_attraction", "park", "national_park", "beach", "hiking_area"],
            "rankPreference": "DISTANCE",
            "maxResultCount": 5  # Limit to a small number of results for speed/cost
        }

        try:
            places_response = requests.post(
                places_endpoint,
                headers=places_headers,
                data=json.dumps(places_payload)
            )
            places_response.raise_for_status()
            places_data = places_response.json()

            if places_data.get('places'):
                # This is the closest and most relevant place found by type
                best_place = places_data['places'][0]

                # Use the display name. Clean up name slightly if needed (optional)
                name = best_place['displayName']['text']
                return name

        except requests.exceptions.HTTPError as e:
            # If we still get a 4xx error (like a quota issue or bad key/config)
            print(f"Places API HTTP Error ({e.response.status_code}). Falling back to Geocoding.")
        except Exception as e:
            # Catch other errors like connection issues or JSON decoding failure
            print(f"Places API General Error ({e.__class__.__name__}). Falling back to Geocoding.")

        # If the Places API failed or returned no results, proceed to Geocoding.

        # -------------------------------------------------------------
        # 2. PRIORITY 2: REVERSE GEOCODING (Geocoding API)
        #    Target: City, Region, Country
        # -------------------------------------------------------------

        geocode_endpoint = "https://maps.googleapis.com/maps/api/geocode/json"
        geocode_params = {
            'latlng': f"{lat},{lng}",
            'key': GOOGLE_API_KEY
        }

        try:
            geocode_response = requests.get(geocode_endpoint, params=geocode_params)
            geocode_response.raise_for_status()
            geocode_data = geocode_response.json()
        except requests.exceptions.RequestException as e:
            # Final failure point
            print(f"Geocoding API Error: {e}")
            return "Unknown_Location_API_Error"

        if geocode_data['status'] != 'OK' or not geocode_data['results']:
            return "Unclassified_Location"

        # Search for City
        for result in geocode_data['results']:
            if any(t in result['types'] for t in ['locality', 'postal_town']):
                for component in result['address_components']:
                    if 'locality' in component['types'] or 'postal_town' in component['types']:
                        return component['long_name']

        # Search for Administrative Area (Region/State)
        for result in geocode_data['results']:
            if any(t in result['types'] for t in ['administrative_area_level_1', 'administrative_area_level_2']):
                for component in result['address_components']:
                    if 'administrative_area_level_1' in component['types'] or 'administrative_area_level_2' in \
                            component['types']:
                        return component['long_name']

        # Final Fallback: Country Name
        for result in geocode_data['results']:
            if 'country' in result['types']:
                for component in result['address_components']:
                    if 'country' in component['types']:
                        return component['long_name']

        return "Unclassified_Location"

    @staticmethod
    def get_categorized_location_name_final(coordinates: tuple) -> str:
        """
        Retrieves a location name using a single Geocoding API call,
        prioritizing specific landmarks/POIs, then falling back to administrative areas.
        """
        lat, lng = coordinates
        geocode_endpoint = "https://maps.googleapis.com/maps/api/geocode/json"

        # Geocoding API Call
        params = {
            'latlng': f"{lat},{lng}",
            'key': GOOGLE_API_KEY
        }

        try:
            response = requests.get(geocode_endpoint, params=params)
            response.raise_for_status()
            data = response.json()
        except requests.exceptions.RequestException as e:
            print(f"Error during API request: {e}")
            return "Unknown_Location_API_Error"

        if data['status'] != 'OK' or not data['results']:
            return "Unclassified_Location"

            # --- 1. PRIORITY: LANDMARK / POINT OF INTEREST (POI) / ROAD ---

            # Define a list of high-priority types for specific naming
        specific_name_types = [
            'point_of_interest', 'establishment', 'park',
            'tourist_attraction', 'national_park',
            'route'  # This is the type for a named road/street
        ]

        # Iterate through all results to find the most specific named feature
        for result in data['results']:
            # Check if the result itself is a specific name type
            if any(t in result['types'] for t in specific_name_types):

                # Now, check the address components within that result for the name
                for component in result['address_components']:

                    # If the component is a specific name type (like 'route' or 'establishment')
                    if any(t in component['types'] for t in specific_name_types):
                        # Return the long name of that specific component
                        return component['long_name']

        # --- 2. PRIORITY: CITY / LOCALITY (The City Grouping) ---
        for result in data['results']:
            if any(t in result['types'] for t in ['locality', 'postal_town']):
                for component in result['address_components']:
                    if 'locality' in component['types'] or 'postal_town' in component['types']:
                        return component['long_name']

        # --- 3. PRIORITY: REGION / ADMINISTRATIVE AREA (The Middle of Nowhere Grouping) ---
        for result in data['results']:
            if any(t in result['types'] for t in ['administrative_area_level_1', 'administrative_area_level_2']):
                for component in result['address_components']:
                    if 'administrative_area_level_1' in component['types'] or 'administrative_area_level_2' in \
                            component['types']:
                        return component['long_name']

        # --- 4. FALLBACK: COUNTRY ---
        for result in data['results']:
            if 'country' in result['types']:
                for component in result['address_components']:
                    if 'country' in component['types']:
                        return component['long_name']

        return "Unclassified_Location"

    @staticmethod
    def get_gmaps_lib(latlon):
        gmaps = googlemaps.Client(key=GOOGLE_API_KEY)

        # Geocoding an address
        geocode_result = gmaps.geocode('1600 Amphitheatre Parkway, Mountain View, CA')

        # Look up an address with reverse geocoding
        reverse_geocode_result = gmaps.reverse_geocode(latlon)
        logging.debug(reverse_geocode_result)

        # Request directions via public transit
        # Get an Address Descriptor of a location in the reverse geocoding response
        # address_descriptor_result = gmaps.. reverse_geocode(latlon, enable_address_descriptor=True)
        logging.debug(gmaps.places_nearby(
            location=latlon,
            # rank_by="distance",
            # keyword='foo',
            radius=800,
        ))

        # logging.debug(address_descriptor_result)


class PictureInfo:
    t_date_time_original = "DateTimeOriginal"
    t_sequence_number = "SequenceNumber"
    t_gps_latitude = "GPSLatitude"
    t_gps_longitude = "GPSLongitude"

    tag_re = re.compile(r"(?P<tag>\w+)\s*:\s*(?P<value>.+)$")

    tags = {
        t_date_time_original: lambda x: datetime.strptime(x, "%Y:%m:%d %H:%M:%S"),
        t_sequence_number: lambda x: int(x),
        t_gps_latitude: lambda x: float(x),
        t_gps_longitude: lambda x: float(x),
    }

    def __init__(self, file):
        self.file = file

        # exiftool -n -DateTimeOriginal -SequenceNumber -GPSLatitude -GPSLongitude
        out = subprocess.check_output(["exiftool", "-n", "-s",
                                       *['-' + t for t in PictureInfo.tags.keys()],
                                       file])

        self.file_tags = {}
        for line in out.decode('utf-8').splitlines():
            tag, value = PictureInfo.tag_re.search(line).groups()
            self.file_tags[tag] = PictureInfo.tags[tag](value)

        self.sequence = None
        if self.t_sequence_number in self.file_tags and self.file_tags[self.t_sequence_number] > 0:
            self.sequence = self.file_tags[self.t_sequence_number]

    def get_extension(self):
        return os.path.splitext(self.file)[-1]

    def get_date_time(self):
        return self.file_tags[self.t_date_time_original]

    def get_sequence_number(self):
        return self.sequence

    def has_latlon(self):
        return self.t_gps_latitude in self.file_tags and self.t_gps_longitude in self.file_tags

    def get_latlon(self):
        return (self.file_tags[self.t_gps_latitude], self.file_tags[self.t_gps_longitude]) if self.has_latlon else None

    def distance(self, latlon1):
        if self.has_latlon():
            return haversine(self.get_latlon(), latlon1)

        return None

    def __str__(self):
        _t = ", ".join([f"{k}={v}" for k, v in self.file_tags.items()])
        return f"{os.path.basename(self.file)} ({self.sequence}); tags: [{_t}];"


def sort_raw():
    from _common import load_configuration
    conf = load_configuration('.rawsort.ini')
    GOOGLE_API_KEY = conf['rawsort']['API_KEY']

    root = os.path.abspath(os.path.expanduser(args.root))
    dest = os.path.abspath(os.path.expanduser(args.destination)) if args.destination else root

    logging.info(f'Scanning {root}, sending to {dest}...\n')

    radius = 3000
    geocluster_map = {}
    not_geo_cluster = []

    def cluster():
        cluster_found = False
        for _center in geocluster_map.keys():
            if info.distance(_center) < radius:
                points = geocluster_map[_center]
                points.append(info)

                _new_center = reduce(lambda l1, l2: (l1[0] + l2[0], l1[1] + l2[1]), [i.get_latlon() for i in points])
                _new_center = (_new_center[0] / len(points), _new_center[1] / len(points))

                geocluster_map[_new_center] = geocluster_map.pop(_center)
                cluster_found = True
                break

        if not cluster_found:
            geocluster_map[info.get_latlon()] = [info]

    for _root, _folders, _files in os.walk(root):
        for file in [os.path.join(_root, f) for f in _files if f[-3:].lower() == 'arw']:
            if args.filter and args.filter not in file:
                continue

            info = PictureInfo(file)
            logging.debug(f"Reading: {info}")
            if info.has_latlon():
                cluster()
            else:
                not_geo_cluster.append(info)

            #
            # day = datetime.strftime(info.get_date_time(), "%d")
            # date = datetime.strftime(info.get_date_time(), "%Y-%m-%dT%H:%M:%S")
            # suffix = f"_{info.get_sequence_number():02d}" if info.get_sequence_number() else ""
            #
            # new_folder = os.path.join(dest, f"{day} - {place}" if place else day)
            # new_filename = f"IMG_{date}{suffix}{info.get_extension()}"
            #
            # destination_file = os.path.join(new_folder, new_filename)
            #
            # # logging.info(f"file: {file} -> {destination_file}")
            # if not args.dry_run:
            #     os.makedirs(new_folder, exist_ok=True)
            #     if os.path.exists(destination_file):
            #         raise ValueError(f"Destination file already exists")
            #
            #     os.rename(file, destination_file)

        if not args.recursive:
            break

    c = 0
    for k, v in geocluster_map.items():
        c += 1
        print(f"{c}. {k}: {len(v)}")
        for i in v:
            print(f"  {i.get_latlon()},")

    print()
    print(len(not_geo_cluster))

    # exiftool \
    #   -geotag gps-track.gpx \
    #   '-geotime<${DateTimeOriginal}-00:01:00' \
    #   -o %d%f.xmp \
    #   *.ARW
    #
    # exiftool -geotag gps-track.gpx '-geotime<${DateTimeOriginal}' -P -overwrite_original *.ARW
    # exiftool -geotag ../gps-track.gpx '-geotime<${DateTimeOriginal}' -api GeoMaxExtSecs=7000 -o %d%f.ARW.xmp *.ARW
    # exiftool -geotag gps-track.gpx '-geotime<${DateTimeOriginal}+00:00:00' -P -overwrite_original IMG_20251108T00040300.ARW
    # exiftool -s -G1 -time:all IMG_20251108T00040300.ARW
    # exiftool -DateTimeOriginal -SequenceNumber -sorted *.ARW

    # def get_place_name(_lat, _lon):
    #     geolocator = Nominatim(user_agent="exif_reader")
    #     location = geolocator.reverse((_lat, _lon), language="en")
    #     return location.address if location else None
    #
    # for f in sorted(os.listdir(target)):
    #     # if os.path.splitext(f)[-1][1:].lower() == 'xmp':
    #     if os.path.splitext(f)[-1][1:].lower() in ['xmp', 'arw']:
    #         # out = subprocess.check_output(["exiv2", "-Pt", "-g", "Exif.GPSInfo", f])
    #         out = subprocess.check_output(["exiv2", "-Pt", "-g", "Xmp.exif.GPS", f])
    #         lines = out.decode('utf-8').splitlines()
    #         # print(f"lines: \n {'\n'.join(lines)}")
    #
    #         raw_lon = lines[1]
    #         raw_lat = lines[2]
    #
    #         def parse_coord(coord):
    #             # Example: "16,20.592501W" or "64,5.823368N"
    #             match = re.match(r"(\d+),(\d+\.?\d*)([NSEW])", coord)
    #             if not match:
    #                 return None
    #
    #             deg = float(match.group(1))
    #             minutes = float(match.group(2))
    #             hemi = match.group(3)
    #
    #             decimal = deg + minutes / 60.0
    #
    #             if hemi in ["S", "W"]:
    #                 decimal = -decimal
    #
    #             return decimal
    #
    #         lon = parse_coord(raw_lon)
    #         lat = parse_coord(raw_lat)
    #
    #         place = get_place_name(lat, lon)
    #         places = place.split(',')
    #
    #         new_dir = os.path.join(target, places[0])
    #
    #         print(f"file: {f} in place {places[0]} -> {place} --> {new_dir} {os.path.join(new_dir, f)}")
    #         os.makedirs(new_dir, exist_ok=True)
    #         os.rename(os.path.join(target, f), os.path.join(new_dir, f))
    #
    # # for image_file in find_images(target):
    # #     folder_count.original_images += 1
    # #     tags = load_exiv2_data(image_file)
    # #
    # #     for line in out.decode('utf-8').splitlines():
    # #         print(f"line gps: {line}")
    # #
    # #     # print(f"file {image_file}: {tags}")


def haversine(latlon1, latlon2):
    r = 6371000  # meters
    d_lat = radians(latlon2[0] - latlon1[0])
    d_lon = radians(latlon2[1] - latlon1[1])

    a = sin(d_lat / 2) ** 2 + cos(radians(latlon1[0])) * cos(radians(latlon2[0])) * sin(d_lon / 2) ** 2
    return 2 * r * asin(sqrt(a))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()

    sort_parser = subparsers.add_parser("sort", help="Sort RAW files with date and location")
    sort_parser.set_defaults(command="sort_raw")
    sort_parser.add_argument("root", help="Root directory")
    sort_parser.add_argument('-r', "--recursive", action='store_true',
                             help="Scan recursively files from root directory")
    sort_parser.add_argument('-d', "--destination", help="Destination directory, root if not specified")
    sort_parser.add_argument('-v', "--verbose", action='store_true', help="Logs more")
    sort_parser.add_argument("--dry-run", action='store_true', help="Don't move/rename files")
    sort_parser.add_argument("-f", '--filter', help="File filter")

    args = parser.parse_args()

    logging.basicConfig(
        format='%(message)s',
        level=logging.DEBUG if args.verbose else logging.INFO)
    logging.getLogger("geopy").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    eval("%s()" % args.command)
