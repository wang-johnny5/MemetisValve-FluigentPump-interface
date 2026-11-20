# MemetisValve-FluigentPump-interface
Interface for controlling up to 2 fluigent pumps and 4 memetis valves using a LINK module and arduino. Includes manual controls and the 2 processes for 2 pumps 4 valves and 1 pump 2 valves. 

Hardware this was built for:
Fluigent Flow EZ pressure controllers connected through a Fluigent LINK module.
A valve box: Arduino Uno plus current regulators
Memetis MVL-22-NC normally closed micro-valves

Installation/Requirements:
Install Python 3.10 or newer
Install pyserial
install Fluigent's official SDK (can be found on their website/github)

Running:
python pump_valve_web.py --sim       runs using simulated pumps and valves
