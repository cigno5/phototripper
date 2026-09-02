import json
import logging
import os
import random
import re
from collections import namedtuple
from math import asin, cos, exp, log, pi, radians, sin, sqrt

import requests

from .common import Context, haversine
from .picture import PictureCluster

AddressSegment = namedtuple("AddressSegment", "name, types")

class _IntersectionAnalysis:
    def __init__(self, circle_c, circle_r, rect_ne, rect_sw):
        self.circle_center = circle_c
        self.circle_radius = circle_r

        num_simulations = 1000
        rect_se = (rect_sw[0], rect_ne[1])
        rect_nw = (rect_ne[0], rect_sw[1])

        # --- PART A: CHECK CONTAINMENT ---
        corners = [rect_sw, rect_se, rect_ne, rect_nw]

        rect_inside_circle = all(haversine(circle_c, latlon) <= circle_r for latlon in corners)

        # 2. Check if Circle is fully inside Rectangle
        # Logic: Center is inside rect AND distance to the closest edge >= radius
        is_center_in_rect = ((rect_sw[0] <= circle_c[0] <= rect_ne[0])
                             and (rect_sw[1] <= circle_c[1] <= rect_ne[1]))

        circle_inside_rect = False
        if is_center_in_rect:
            # Distance to top/bottom borders (Latitude difference)
            # We approximate 1 degree lat ~= 111 km
            dist_to_top = haversine(circle_c, (rect_ne[0], circle_c[1]))
            dist_to_bottom = haversine(circle_c, (rect_sw[0], circle_c[1]))

            # Distance to left/right borders (Longitude difference at current lat)
            dist_to_left = haversine(circle_c, (circle_c[0], rect_sw[1]))
            dist_to_right = haversine(circle_c, (circle_c[0], rect_ne[1]))

            circle_inside_rect = min(dist_to_top, dist_to_bottom, dist_to_left, dist_to_right) >= circle_r

        rect_c = ((rect_sw[0] + rect_ne[0]) / 2, (rect_sw[1] + rect_ne[1]) / 2)

        # 2. Measure distance from circle center to that closest point
        self.distance_to_center = haversine(circle_c, rect_c)

        # Check if the two shapes don't intersect at all
        shapes_dont_intersect = self.distance_to_center > circle_r

        if rect_inside_circle:
            self.status = "rect-inside-circle"
        elif circle_inside_rect:
            self.status = "circle-inside-rect"
        elif shapes_dont_intersect:
            self.status = "not-intersecting"
        else:
            self.status = "intersecting"

        # --- PART B: CALCULATE COVERAGE (Monte Carlo) ---
        # Calculate raw areas
        self.circle_area = pi * (circle_r ** 2)
        self.rect_area = _rect_area(rect_sw, rect_ne)

        self.smaller_shape_area = min(self.circle_area, self.rect_area)
        self.bigger_shape_area = max(self.circle_area, self.rect_area)

        # Intersection Calculation
        # We generate random points inside the RECTANGLE and check if they are in the CIRCLE.
        points_in_circle = 0

        for _ in range(num_simulations):
            # Generate random point inside rectangle
            rand = (random.uniform(rect_sw[0], rect_ne[0]), random.uniform(rect_sw[1], rect_ne[1]))

            # Check distance to center
            if haversine(rand, circle_c) <= circle_r:
                points_in_circle += 1

        # Fix the simulation for the circle within a very big rect
        if circle_inside_rect and points_in_circle == 0:
            points_in_circle = 1

        # The fraction of the rectangle covered by the circle
        fraction_rect_covered = points_in_circle / num_simulations
        self.intersection_area = fraction_rect_covered * self.rect_area

    def get_size_ratio(self):
        if self.status == "intersecting":
            return self.intersection_area / self.circle_area
        elif self.status == 'not-intersecting' or self.smaller_shape_area == 0:
            return 0
        else:
            return self.bigger_shape_area / self.smaller_shape_area

    def get_size_factor(self):
        ratio = self.get_size_ratio()
        return 0 if ratio == 0 else ratio if ratio <= 1 else 1 / ratio

    def get_center_factor(self):
        # def get_bounded_gaussian(distance, radius, min_score_at_radius=0.7):
        # Calculate the center point of the gaussian distribution
        min_score_at_radius = 0.5
        if self.distance_to_center == 0:
            return 1.0

        # 1. Calculate the required sigma for the boundary condition
        # sigma = sqrt( radius^2 / (-2 * ln(min_score)) )
        sigma = sqrt((self.circle_radius / 2) ** 2 / (-2 * log(min_score_at_radius)))

        # 2. Calculate Gaussian factor
        return 1 + exp(-(self.distance_to_center ** 2) / (2 * sigma ** 2)) if (2 * sigma ** 2) != 0 else 0
        # sigma = 5
        # return math.exp(-(self.distance_to_center ** 2) / (2 * sigma ** 2))

    def __str__(self):
        return f"Status: {self.status}; Circle area: {self.circle_area}; Rect area: {self.rect_area}; Intersection area: {self.intersection_area}"


class GeoTraits:
    T_POI = 'point_of_interest'
    T_LOCALITY = 'locality'
    T_TOURIST_ATTRACTION = 'tourist_attraction'
    T_ROUTE = 'route'

    INTERESTING_TYPES = [T_TOURIST_ATTRACTION, T_POI, T_LOCALITY]

    def __init__(self, center, radius, geo_service, geo_data):
        self.service = geo_service
        if geo_service == 'geocode':
            _t_address = 'formatted_address'
            _t_address_components = 'address_components'
            _lat, _lon = 'lat', 'lng'
            _high, _low = 'northeast', 'southwest'
            _viewport = geo_data['geometry']['viewport']
            _t_long_name = 'long_name'
            _t_short_name = 'short_name'
        elif geo_service == 'gplaces':
            _t_address = 'formattedAddress'
            _t_address_components = 'addressComponents'
            _lat, _lon = 'latitude', 'longitude'
            _high, _low = 'high', 'low'
            _viewport = geo_data['viewport']
            _t_long_name = 'longText'
            _t_short_name = 'shortText'
        else:
            raise NotImplementedError(f"Service {geo_service} not implemented")

        self.center = center
        self.radius = radius
        self.address = geo_data[_t_address]
        self.types = geo_data['types']

        self.primary_type = geo_data['primaryType'] if geo_service == 'gplaces' and 'primaryType' in geo_data else None
        self.display_name = geo_data['displayName']['text'] if geo_service == 'gplaces' else geo_data[_t_address]

        self.viewport = (_viewport[_high][_lat], _viewport[_high][_lon]), (_viewport[_low][_lat], _viewport[_low][_lon])

        self.address_chain: list[AddressSegment] = [
            AddressSegment(
                c[_t_short_name] if _t_short_name in c else c[_t_long_name],
                c['types'] if 'types' in c else []
            )
            for c in geo_data[_t_address_components]
        ]

        self.intersection_analysis = _IntersectionAnalysis(self.center, self.radius, 
                                                           self.viewport[0], self.viewport[1])

    def get_place_score(self, use_size_factor, use_center_factor):
        def x_score(base_score, _type):
            if _type in GeoTraits.INTERESTING_TYPES:
                return base_score * 10 * (len(GeoTraits.INTERESTING_TYPES) - GeoTraits.INTERESTING_TYPES.index(_type))
            else:
                return 0

        score = x_score(1000, self.primary_type)
        for _type in self.types:
            score += x_score(100, _type)

        for _address_comp in self.address_chain:
            for _type in _address_comp.types:
                if _type in GeoTraits.INTERESTING_TYPES:
                    score += x_score(10, _type)

        if use_size_factor:
            score *= self.intersection_analysis.get_size_factor()

        if use_center_factor:
            score *= self.intersection_analysis.get_center_factor()

        return score

    def get_place_name(self):
        def _p(_tp):
            return next((c.name for c in self.address_chain if _tp in c.types), None)

        return next((n for n in [self.display_name] if n and self.primary_type in GeoTraits.INTERESTING_TYPES),
                    next((_p(it) for it in GeoTraits.INTERESTING_TYPES if it in self.types and _p(it)),
                         next((_p(it) for it in GeoTraits.INTERESTING_TYPES if _p(it)), None)))

    def _is_something(self, tp):
        return tp in self.types or tp == self.primary_type

    def __str__(self):
        return f"""Place name............: {self.get_place_name()}
  - Service...........: {self.service}
  - Place score.......: {self.get_place_score(False, False)}
  - Place score SF....: {self.get_place_score(True, False)}
  - Place score CF....: {self.get_place_score(False, True)}
  - Place score SF+CF.: {self.get_place_score(True, True)}
  - Intersection analysis
    - Size factor.....: {self.intersection_analysis.get_size_factor()}
    - Center factor...: {self.intersection_analysis.get_center_factor()}
  - Address...........: {self.address}
  - Primary type......: {self.primary_type}
  - Types.............: {', '.join(self.types)}
  - Geometry..........: {self.viewport}
  - Address Chain.....:
""" + '\n'.join([f"    - {str(c)}" for c in self.address_chain])


class PictureLocation:
    def __init__(self, cluster: PictureCluster):
        self.cluster = cluster
        self.ctx = Context.get()
        logging.debug(f"Loading location information for cluster {cluster.name} ({cluster.center})...")

        self.geocode_traits = _geo_decode(cluster, 'geocode')
        self.gplaces_traits = _geo_decode(cluster, 'gplaces')

    def first_by_service(self, service) -> GeoTraits:
        def flt(t: GeoTraits):
            return t.get_place_name() is not None

        _traits = None

        if service == 'gplaces':
            _traits = self.gplaces_traits

        if service == 'geocode':
            _traits = self.geocode_traits

        if self.ctx.logging_settings.debug:
            c = 0
            for t in _traits:
                c += 1
                logging.info(f"Service {service} - Trait {c:2d}\n{str(t)}")

        return next(iter(filter(flt, _traits)), None)

    def first_by_score(self, use_size_factor=False, use_center_factor=False, services=None) -> GeoTraits:
        def _srt(t: GeoTraits):
            return t.get_place_score(use_size_factor, use_center_factor)

        def _flt(t: GeoTraits):
            return _srt(t) > 0

        _traits = []
        if services is None or 'geocode' in services:
            _traits += self.geocode_traits
        if services is None or 'gplaces' in services:
            _traits += self.gplaces_traits

        return next(iter(sorted(filter(_flt, _traits), key=_srt, reverse=True)), None)


def _rect_area(sw, ne):
    # Height: Distance from SW lat to NE lat (along same longitude)
    height = haversine(sw, ne)
    # Width: Distance from SW lon to NE lon (along middle latitude for better accuracy)
    mid_lat = (sw[0] + ne[0]) / 2
    width = haversine((mid_lat, sw[1]), (mid_lat, ne[1]))
    return height * width


def _geo_decode(cluster: PictureCluster, service):
    assert service in ['geocode', 'gplaces', 'nominatim']

    ctx = Context.get()

    logging.debug(f"Loading data for cluster {cluster.name} ({cluster.center}) through service {service}...")

    _cache_folder = os.path.join(ctx.location_settings.cache_dir, 'phototripping')
    os.makedirs(_cache_folder, exist_ok=True)

    # Search a cache file that is within half the search radius from the cluster's center
    def find_cache_file():
        _c_re = re.compile(r'\((?P<lat>-?[\d.]+),\s(?P<lon>-?[\d.]+)\)-(?P<service>\w+)\.json')
        for _cache_file in os.listdir(_cache_folder):
            _m = _c_re.match(_cache_file)
            if _m and _m.group('service') == service:
                cache_center = (float(_m.group('lat')), float(_m.group('lon')))
                if haversine(cluster.center, cache_center) < ctx.location_settings.search_radius / 4:
                    return _cache_file

        return f'{cluster.center}-{service}.json'

    _data_file = os.path.join(_cache_folder, find_cache_file())

    try:
        with open(_data_file, 'r') as df:
            data = json.load(df)
            logging.debug(f" > data loaded from cache file {_data_file}")

    except FileNotFoundError:
        with open(_data_file, 'w') as df:
            if service == 'geocode':
                data = ctx.gmaps.reverse_geocode(cluster.center)
                logging.debug(" > data loaded from Google reverse geocode API")
            elif service == 'gplaces':
                headers = {
                    'Content-Type': 'application/json',
                    'X-Goog-Api-Key': ctx.api_key,
                    'X-Goog-FieldMask': 'places.displayName,places.formattedAddress,places.addressComponents,'
                                        'places.primaryType,places.types,places.viewport'
                }

                json_data = {
                    'includedTypes': ['tourist_attraction', 'historical_landmark', 'locality',
                                      'administrative_area_level_1', ],
                    "maxResultCount": 10,
                    'locationRestriction': {
                        'circle': {
                            'center': {
                                'latitude': cluster.center[0],
                                'longitude': cluster.center[1],
                            },
                            'radius': max(cluster.radius, ctx.location_settings.search_radius),
                        },
                    },
                }

                r = requests.post('https://places.googleapis.com/v1/places:searchNearby',
                                  headers=headers,
                                  json=json_data)
                r.raise_for_status()
                data = r.json()['places']
                logging.debug(" > data loaded from Google Places 'Search Nearby' API")
            elif service == 'nominatim':
                raise NotImplementedError('Nominatim not implemented yet')

            logging.debug(f" > data stored in cache file {_data_file}")
            json.dump(data, df)

    return [GeoTraits(cluster.center, cluster.radius, service, obj) for obj in data]

