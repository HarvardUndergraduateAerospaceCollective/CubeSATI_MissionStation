
    functional:
    
    #TODO: rework live/static mode, actual in situ implementation will be "semi"-live mode, with 
    # periodic updates as data comes in from mqtt listener and celestrak (capped at every 2hrs),
    # and interperlating ONLY ground track/position data between these updates. 

    #TODO: add more info about packets, time since last packets, etc... specific to this semi-live mode

    #TODO: either entirely replace, or much clean up the "fallback" data, which is just false and only really for testing


    aesthetics:

    #TODO: rework matplot lib to be "true" fullscreen, without border or bars, just the plot


    prep for real data:

    #TODO: change cat_nr from ISS to actual
    #TODO: mqtt login info (from telegram?)